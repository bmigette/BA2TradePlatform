"""The family-independent core of the action / step records (``action_record``).

A tiny non-option family is built on :class:`FrameRecorder` to prove the core stands on its own:
one record per span, the effect / record notes, re-entry, the error path, the no-op paths, the
recorder-failure policy, redaction, the codec guard and the expert-scoped DB read set. The option
family on top of it is pinned by ``test_option_action_record``.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.db import add_instance
from ba2_common.core.models import ExpertInstance, ExpertSetting, TradingOrder, Transaction
from ba2_common.core.replay import action_record as core, codec
from ba2_common.core.replay.context import CaptureContext, CaptureHealth, capture_scope, current_capture, use_capture_context
from ba2_common.core.replay.schemas import ReplayStatus
from ba2_common.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from tests.test_max_loss_persisted_at_submit import _own_db  # noqa: F401  (autouse DB)

UTC_NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self):
        self.health = CaptureHealth()
        self.submitted = []

    def submit(self, record, objects=None, observations=()):
        self.submitted.append((record, dict(objects or {}), list(observations)))


def _meta():
    return {
        "analysis_id": "a1", "attempt_id": "att-1", "session_id": "s1",
        "expert_class": "X", "expert_instance_id": 7, "symbol": "XYZ",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
        "scheduled_at": UTC_NOW, "started_at": UTC_NOW,
    }


class _ToyRecorder(core.FrameRecorder):
    provider = "toy_step"
    version = 7
    family = "toy step"

    def method_name(self):
        return "toy"

    def identity(self):
        return {"name": self.action.name}

    def build_inputs(self):
        if getattr(self.action, "boom_inputs", False):
            raise RuntimeError("inputs")
        return {"name": self.action.name}

    def collect_db_rows(self):
        return [{"model": "ExpertInstance", "row": {"id": 1}}], ["x_api_key"]


def _toy(name="a"):
    return SimpleNamespace(name=name)


def _record(subject):
    context = current_capture()
    if context is None or context.is_replay:
        return core.NOOP_CONTEXT
    return _ToyRecorder(subject, context)


def _obs(store):
    return [o for o in store.submitted[0][2] if o.observation.provider == "toy_step"]


def _payload(pending):
    encoded = codec.encode(pending.payload)
    return codec.decode(encoded.kind, encoded.data, encoded.meta)


def test_one_span_records_one_observation_with_the_documented_payload_shape():
    store = _Store()
    subject = _toy()
    with capture_scope(store, _meta()):
        with _record(subject) as handle:
            core.note_effect("submit_order", quantity=3)
            core.note_record("entry_record", {"k": 1})
            handle.result = {"success": True, "message": "ok", "data": {"entry_record": {"k": 1}, "x": 2}}
    (obs,) = _obs(store)
    assert obs.observation.method == "toy"
    assert obs.observation.request_identity == {"name": "a"}
    payload = _payload(obs)
    assert payload["version"] == 7
    assert set(payload) == {"version", "tape_span", "inputs", "db_rows", "outcome"}
    assert set(payload["tape_span"]) == {"start", "clock_start", "clock_end"}
    assert payload["inputs"] == {"name": "a", "redacted_setting_keys": ["x_api_key"]}
    out = payload["outcome"]
    assert (out["status"], out["success"], out["message"]) == ("returned", True, "ok")
    assert out["entry_record"] == {"k": 1} and out["result_data"] == {"x": 2, "entry_record": {"k": 1}}
    assert out["submits"] == [{"method": "submit_order", "quantity": 3}]
    assert getattr(core._tls, "frame", None) is None


def test_the_same_subject_re_entered_is_one_record_and_effects_land_in_it():
    store = _Store()
    subject = _toy()
    with capture_scope(store, _meta()):
        with _record(subject):
            with _record(subject):
                core.note_effect("adjust_sl", price=1.0)
    (obs,) = _obs(store)
    assert [s["method"] for s in _payload(obs)["outcome"]["submits"]] == ["adjust_sl"]


def test_a_different_nested_subject_gets_its_own_frame_and_the_outer_one_is_restored():
    store = _Store()
    outer, inner = _toy("outer"), _toy("inner")
    with capture_scope(store, _meta()):
        with _record(outer):
            core.note_effect("a")
            with _record(inner):
                core.note_effect("b")
            core.note_effect("c")
    by_name = {o.observation.request_identity["name"]: _payload(o)["outcome"]["submits"]
               for o in _obs(store)}
    assert [s["method"] for s in by_name["outer"]] == ["a", "c"]
    assert [s["method"] for s in by_name["inner"]] == ["b"]


def test_an_exception_is_recorded_as_error_and_propagates_unchanged():
    store = _Store()
    with capture_scope(store, _meta()):
        with pytest.raises(ValueError, match="nope"):
            with _record(_toy()):
                raise ValueError("nope")
    (obs,) = _obs(store)
    out = _payload(obs)["outcome"]
    assert out["status"] == "error" and "nope" in out["error"]
    assert getattr(core._tls, "frame", None) is None


def test_outside_a_capture_scope_and_in_replay_nothing_is_recorded():
    assert _record(_toy()) is core.NOOP_CONTEXT
    core.note_effect("submit_order")          # no frame: no-op, no raise
    core.note_record("x", 1)
    with use_capture_context(CaptureContext.for_replay(analysis_id="a1", clock_reads=[])):
        assert _record(_toy()) is core.NOOP_CONTEXT


def test_a_failing_inputs_build_is_counted_and_the_span_is_not_recorded():
    store = _Store()
    subject = _toy()
    subject.boom_inputs = True
    with capture_scope(store, _meta()):
        with _record(subject) as handle:
            handle.result = None
            core.note_effect("submit_order")          # no frame was installed
    assert not _obs(store)
    assert store.health.other == 1
    assert getattr(core._tls, "frame", None) is None


def test_a_failing_observation_write_is_counted_and_never_reaches_the_caller(monkeypatch):
    store = _Store()
    monkeypatch.setattr(_ToyRecorder, "emit", lambda self, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    with capture_scope(store, _meta()):
        with _record(_toy()):
            pass
    assert store.health.other == 1
    assert getattr(core._tls, "frame", None) is None


def test_an_unencodable_effect_becomes_a_named_marker_and_a_counted_failure():
    store = _Store()
    with capture_scope(store, _meta()):
        with _record(_toy()):
            core.note_effect("submit_order", handle=lambda: 1)
    (obs,) = _obs(store)
    (effect,) = _payload(obs)["outcome"]["submits"]
    assert effect["method"] == "submit_order" and "unencodable" in effect


def test_replay_effects_installs_a_frame_without_recording_and_restores_the_previous_one():
    sentinel = core.Frame("previous")
    core._tls.frame = sentinel
    try:
        with core.replay_effects(_toy()) as frame:
            core.note_effect("close_transaction", transaction_id=5)
            core.note_record("exit_record", {"r": 1})
        assert core._tls.frame is sentinel
    finally:
        core._tls.frame = None
    outcome = core.build_outcome(None, {"success": True, "message": "m", "data": {}}, frame,
                                 lambda value, label: value)
    assert outcome["submits"] == [{"method": "close_transaction", "transaction_id": 5}]
    assert outcome["exit_record"] == {"r": 1}


def test_build_outcome_skips_bulky_result_keys_and_prefers_the_result_entry_record():
    frame = core.Frame(None)
    frame.records["entry_record"] = {"from": "frame"}
    result = {"success": False, "message": "refused",
              "data": {"entry_record": {"from": "result"}, "evaluation_details": {"big": 1}, "n": 1}}
    out = core.build_outcome(None, result, frame, lambda value, label: value)
    assert out["entry_record"] == {"from": "result"}
    assert out["result_data"] == {"entry_record": {"from": "result"}, "n": 1}
    attr_result = SimpleNamespace(success=True, message=None, data=None)
    assert core.build_outcome(None, attr_result, core.Frame(None), lambda v, l: v)["success"] is True


def test_version_gate_and_tape_span():
    assert core.check_version({"version": 1}, (1, 2))
    assert not core.check_version({"version": 3}, (1, 2))
    assert core.tape_span(4, 2, 9) == {"start": 4, "clock_start": 2, "clock_end": 9}


def test_account_declarations_keep_string_tags_only():
    account = SimpleNamespace(OPTION_GREEKS_SOURCE="broker", OTHER=object())
    assert core.account_declarations(account, ("OPTION_GREEKS_SOURCE", "OTHER", "MISSING")) == {
        "OPTION_GREEKS_SOURCE": "broker", "OTHER": None, "MISSING": None}


def test_secret_keys_are_filtered_by_token():
    assert core.is_secret("openai_api_key") and core.is_secret("SomeToken")
    assert not core.is_secret("max_virtual_equity_per_instrument_percent")


def test_expert_rows_hold_instance_non_secret_settings_and_capital_holding_set_only():
    expert_id = add_instance(ExpertInstance(account_id=1, expert="X", virtual_equity_pct=80.0))
    add_instance(ExpertSetting(instance_id=expert_id, key="sizing_mode", value_str="fixed", value_float=None))
    add_instance(ExpertSetting(instance_id=expert_id, key="finnhub_api_key", value_str="sk", value_float=None))

    def txn(symbol, status):
        return add_instance(Transaction(symbol=symbol, quantity=1, side=OrderDirection.BUY,
                                        status=status, expert_id=expert_id))

    open_id = txn("AAA", TransactionStatus.OPENED)
    closing_id = txn("BBB", TransactionStatus.CLOSING)
    closed_id = txn("CCC", TransactionStatus.CLOSED)
    order_id = add_instance(TradingOrder(account_id=1, symbol="AAA", quantity=1, side=OrderDirection.BUY,
                                         order_type=OrderType.MARKET, status=OrderStatus.FILLED,
                                         transaction_id=open_id))
    rows = {}

    def add(obj):
        if obj is not None:
            rows[(type(obj).__name__, obj.id)] = core.dump_row(obj)

    redacted = core.collect_expert_rows(add, expert_id)
    assert redacted == ["finnhub_api_key"]
    keys = set(rows)
    assert ("ExpertInstance", expert_id) in keys
    assert {("Transaction", open_id), ("Transaction", closing_id), ("TradingOrder", order_id)} <= keys
    assert ("Transaction", closed_id) not in keys
    assert {r["row"]["key"] for r in rows.values() if r["model"] == "ExpertSetting"} == {"sizing_mode"}
    payload = {"db_rows": core.sorted_rows(rows)}
    assert {type(i).__name__ for i in core.db_row_instances(payload)} >= {"ExpertInstance", "TradingOrder"}
    assert core.collect_expert_rows(add, None) == []


def test_the_option_family_is_built_on_the_core_with_unchanged_public_names():
    from ba2_common.core.replay import option_action_record as oar
    assert oar._tls is core._tls
    assert oar._NOOP_CONTEXT is core.NOOP_CONTEXT
    assert issubclass(oar._Recorder, core.FrameRecorder)
    assert (oar.OPTION_ACTION_PROVIDER, oar.RECORD_VERSION, oar.READABLE_VERSIONS) == (
        "option_action", 2, (1, 2))
    assert oar.build_outcome is core.build_outcome and oar.db_row_instances is core.db_row_instances
