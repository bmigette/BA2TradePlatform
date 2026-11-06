"""``observe_provider(record_errors=True)``: a raised read is an observation (option taps, opt-in).

A tapped read that RAISED used to leave no trace, so an action that handled it gracefully (skip the
candidate) replayed as ``missing_capture``. With ``record_errors`` the exception is recorded as an
``error`` observation and then re-raised UNCHANGED; the stock taps (default False) are untouched.
"""
from datetime import datetime, timezone

import pytest

from ba2_common.core.replay import codec
from ba2_common.core.replay.context import CaptureHealth, capture_scope
from ba2_common.core.replay.observe import observe_provider
from ba2_common.core.replay.recorded_errors import (
    RecordedProviderError, error_payload, rebuild_error,
)
from ba2_common.core.replay.schemas import ReplayStatus

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


class _ThirdPartyError(Exception):
    pass


_ThirdPartyError.__module__ = "alpaca.common.exceptions"


class _Outer:
    class Inner(Exception):
        pass


def _tapped(raiser, **kw):
    @observe_provider("p", "m", identity=lambda a: {"x": a["x"]}, **kw)
    def read(x):
        raise raiser

    return read


def test_a_raised_read_is_recorded_as_an_error_observation_and_reraised_unchanged():
    boom = ValueError("no such contract")
    store = _Store()
    with capture_scope(store, _meta()):
        with pytest.raises(ValueError) as caught:
            _tapped(boom, record_errors=True)(1)
        assert caught.value is boom
    (pending,) = store.submitted[0][2]
    assert pending.observation.response_class == ReplayStatus.RESPONSE_ERROR
    assert pending.observation.request_identity == {"x": 1}
    encoded = codec.encode(pending.payload)
    assert codec.decode(encoded.kind, encoded.data, encoded.meta) == {
        "exc_type": "ValueError", "module": "builtins", "message": "no such contract"}


def test_a_qualified_name_is_recorded_for_a_nested_class():
    assert error_payload(_Outer.Inner("m"))["exc_type"] == "_Outer.Inner"


def test_without_the_opt_in_nothing_is_recorded_for_a_raise():
    store = _Store()
    with capture_scope(store, _meta()):
        with pytest.raises(ValueError):
            _tapped(ValueError("x"))(1)
    assert store.submitted[0][2] == []


def test_outside_a_scope_a_raise_is_untouched():
    with pytest.raises(ValueError):
        _tapped(ValueError("x"), record_errors=True)(1)


def test_an_identity_failure_never_replaces_the_original_exception():
    @observe_provider("p", "m", identity=lambda a: 1 / 0, record_errors=True)
    def read(x):
        raise KeyError("orig")

    store = _Store()
    with capture_scope(store, _meta()) as ctx:
        with pytest.raises(KeyError, match="orig"):
            read(1)
        assert ctx.capture_failures
    assert store.submitted[0][2] == []


def test_rebuild_a_builtin_with_one_message_argument():
    err = rebuild_error({"exc_type": "ValueError", "module": "builtins", "message": "m"})
    assert type(err) is ValueError and str(err) == "m"


def test_rebuild_a_ba2_common_class():
    from ba2_common.core.failure_modes import UnmeasuredValue
    err = rebuild_error(error_payload(UnmeasuredValue("no price")))
    assert type(err) is UnmeasuredValue and str(err) == "no price"


def test_a_third_party_class_replays_as_a_recorded_provider_error():
    err = rebuild_error(error_payload(_ThirdPartyError("rate limited")))
    assert type(err) is RecordedProviderError
    assert err.type_name == "alpaca.common.exceptions._ThirdPartyError"
    assert "rate limited" in str(err)


def test_a_class_that_cannot_take_one_message_is_a_recorded_provider_error():
    err = rebuild_error(error_payload(UnicodeDecodeError("utf-8", b"x", 0, 1, "bad")))
    assert type(err) is RecordedProviderError


def test_a_class_whose_message_does_not_round_trip_is_a_recorded_provider_error():
    err = rebuild_error(error_payload(KeyError("k")))     # str(KeyError('k')) == "'k'"
    assert type(err) is RecordedProviderError
