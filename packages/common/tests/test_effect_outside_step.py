"""I1: an account effect inside a decision pass but OUTSIDE an execute frame is recorded, not dropped.

Examples: dependent TP/SL legs submitted by ``_check_all_waiting_trigger_orders`` in the enter pass,
``_fail_unsent_entry``, ``refresh_orders``-driven changes. They land in the pass-level list
``effects_outside_steps`` of the pass summary.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from ba2_common.core.replay import action_record as core
from ba2_common.core.replay.context import CaptureHealth, capture_scope, current_capture
from ba2_common.core.replay.decision_record import pass_summary
from ba2_common.core.replay.schemas import ReplayStatus
from tests._account_tap_doubles import RecordingStore, StubAccount
from tests.test_effect_tap_nested import _Step, _OptionLikeStep, _txn

UTC_NOW = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)


def _meta(kind="decision_pass"):
    return {
        "analysis_id": "p1", "attempt_id": "att-1", "session_id": "s1",
        "expert_class": "X", "expert_instance_id": 7, "symbol": "",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
        "scheduled_at": None, "started_at": UTC_NOW,
        "branch_flags": {"record_kind": kind, "pass": "enter-market", "trigger": "scheduler"},
    }


class _Acct(StubAccount):
    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        if new_tp_price == -1:
            raise RuntimeError("broker refused")
        return True


def test_an_effect_outside_any_step_lands_in_the_pass_summary():
    store = RecordingStore()
    with capture_scope(store, _meta()):
        _Acct().adjust_tp_sl(_txn(5), new_tp_price=12.5, source="trigger-check")
        summary = pass_summary(current_capture())
    (entry,) = summary["effects_outside_steps"]
    assert entry["method"] == "adjust_tp_sl" and entry["call"]["new_tp_price"] == 12.5
    assert entry["returned"] is True
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) == 0


def test_a_failing_effect_outside_a_step_is_recorded_with_its_error_and_still_raises():
    store = RecordingStore()
    with capture_scope(store, _meta()):
        try:
            _Acct().adjust_tp_sl(_txn(5), new_tp_price=-1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("must re-raise")
        (entry,) = pass_summary(current_capture())["effects_outside_steps"]
    assert "broker refused" in entry["error"]


def test_an_effect_inside_a_step_is_not_duplicated_outside():
    store = RecordingStore()
    with capture_scope(store, _meta()):
        with _Step(SimpleNamespace(), current_capture()):
            _Acct().adjust_tp_sl(_txn(5), new_tp_price=1.0)
        assert pass_summary(current_capture())["effects_outside_steps"] == []


def test_a_family_frame_that_notes_its_own_submits_is_not_a_gap():
    store = RecordingStore()
    with capture_scope(store, _meta()):
        with _OptionLikeStep(SimpleNamespace(), current_capture()):
            _Acct().adjust_tp_sl(_txn(5), new_tp_price=1.0)
        assert pass_summary(current_capture())["effects_outside_steps"] == []


def test_an_analysis_context_is_unchanged():
    store = RecordingStore()
    with capture_scope(store, _meta(kind="analysis")):
        _Acct().adjust_tp_sl(_txn(5), new_tp_price=1.0)
        assert current_capture().decision_ledger == {}


# --- M2 / M3: what an effect records when it raises -----------------------------------------------
class _Raising(StubAccount):
    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        raise self._exc


def test_a_base_exception_is_recorded_then_reraised():
    """CancelledError-style exits: the call may have reached the broker, so the attempt is recorded."""
    class _Cancelled(BaseException):
        pass

    store = RecordingStore()
    with capture_scope(store, _meta()):
        try:
            _Raising(_Cancelled("cancelled mid-call")).adjust_tp_sl(_txn(5), new_tp_price=1.0)
        except _Cancelled:
            pass
        else:
            raise AssertionError("must re-raise")
        (entry,) = pass_summary(current_capture())["effects_outside_steps"]
    assert "cancelled mid-call" in entry["error"]


def test_a_credential_in_the_error_text_is_redacted():
    store = RecordingStore()
    boom = RuntimeError("401 for https://x/y?apikey=SUPERSECRET123&symbol=AAPL token=abc999")
    with capture_scope(store, _meta()):
        try:
            _Raising(boom).adjust_tp_sl(_txn(5), new_tp_price=1.0)
        except RuntimeError:
            pass
        (entry,) = pass_summary(current_capture())["effects_outside_steps"]
    assert "SUPERSECRET123" not in entry["error"] and "abc999" not in entry["error"]
    assert "REDACTED" in entry["error"] and "symbol=AAPL" in entry["error"]
