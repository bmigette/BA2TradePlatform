"""Stock decision-step records (``decision_record``): payload shape, views, redaction, failure policy.

The end-to-end proof (real passes, capture on == capture off) is ``tests/test_decision_pass_capture_roundtrip``.
Here: each recorder on stubs, the pure views, the secret filter on the pass state, the unencodable marker,
and that a recorder defect is counted and never reaches the decision.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.db import add_instance
from ba2_common.core.models import (
    ExpertInstance, ExpertRecommendation, ExpertSetting, TradingOrder, Transaction,
)
from ba2_common.core.replay import codec, decision_record as dr
from ba2_common.core.replay.account_taps import EFFECT_METHODS  # noqa: F401  (tap tables import cleanly)
from ba2_common.core.replay.action_record import note_account_effect
from ba2_common.core.replay.context import CaptureContext, CaptureHealth, capture_scope, use_capture_context
from ba2_common.core.replay.schemas import ReplayStatus
from ba2_common.core.types import (
    ExpertEventRuleType, OrderDirection, OrderRecommendation, OrderStatus, OrderType, RiskLevel, TimeHorizon,
    TransactionStatus,
)
from tests.test_max_loss_persisted_at_submit import _own_db  # noqa: F401  (autouse DB)

UTC_NOW = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self):
        self.health = CaptureHealth()
        self.submitted = []

    def submit(self, record, objects=None, observations=()):
        self.submitted.append((record, dict(objects or {}), list(observations)))


def _meta(**kw):
    meta = {
        "analysis_id": "decision-7-a", "attempt_id": "att-1", "session_id": "s1",
        "expert_class": "FMPRating", "expert_instance_id": 7, "symbol": "",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET, "scheduled_at": None, "started_at": UTC_NOW,
        "branch_flags": {"record_kind": "decision_pass", "pass": "enter-market", "trigger": "scheduler",
                         "host_timezone": {"name": None, "utc_offset_seconds": 0}},
    }
    meta.update(kw)
    return meta


def _payload(pending):
    encoded = codec.encode(pending.payload)
    return codec.decode(encoded.kind, encoded.data, encoded.meta)


def _steps(store, kind=None):
    return [(o, _payload(o)) for o in store.submitted[0][2]
            if o.observation.provider == dr.DECISION_PROVIDER
            for o in [o.__class__ and o] if kind is None or o.observation.method == kind]


def _rec(symbol="AAPL", id_=1):
    return ExpertRecommendation(
        id=id_, instance_id=7, symbol=symbol, recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=12.0, price_at_date=100.0, details="d", confidence=80.0,
        risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM, created_at=UTC_NOW)


class _Evaluator:
    def __init__(self, details=None, summaries=None):
        self.account = SimpleNamespace(id=3, OPTION_GREEKS_SOURCE=None)
        self.existing_transactions = [SimpleNamespace(id=11), SimpleNamespace(id=12)]
        self.trade_actions = []
        self._details = details if details is not None else {"condition_evaluations": [], "rule_evaluations": []}

    def get_evaluation_details(self):
        return self._details

    def _get_action_type_from_action(self, action):
        return action


# --------------------------------------------------------------------------- outside a pass
def test_every_entry_point_is_a_noop_outside_a_decision_pass():
    assert dr.record_rules(_Evaluator(), "AAPL", _rec(), 1, None) is dr._NOOP_CONTEXT
    assert dr.record_size(object(), 7, [(object(), object())], None) is dr._NOOP_CONTEXT
    assert dr.record_execute(_Evaluator(), "AAPL", _rec(), True) is dr._NOOP_CONTEXT
    dr.note_gate("AAPL", "g", True)
    dr.note_pass_error("x", RuntimeError("y"))
    dr.note_funded("AAPL", 1, 2)
    dr.pass_skip("r")
    dr.pass_discard("r")
    dr.pass_fail("r")
    dr.note_selection([_rec()])
    dr.note_sizing_run([], {})
    assert dr.record_pass_state(expert=None, expert_instance=None, pass_name="x", lookback_days=1,
                                cutoff_time=None, decision_time=None, account=None, ruleset_id=None,
                                fetched_recommendations=[], selected_recommendations=[]) is None


def test_an_analysis_context_is_never_touched_by_the_pass_helpers():
    """A note made while an ANALYSIS scope is current must not set that analysis' outcome."""
    meta = _meta(branch_flags={})
    with capture_scope(_Store(), meta) as context:
        dr.pass_skip("automation off")
        dr.pass_discard("lock_busy")
        dr.pass_fail("boom")
        assert not context.has_outcome and context.discard_reason is None
        assert dr.record_rules(_Evaluator(), "AAPL", _rec(), 1, None) is dr._NOOP_CONTEXT
        context.set_skip("done")


def test_a_replaying_context_records_nothing():
    context = CaptureContext.for_replay(analysis_id="a", clock_reads=[], branch_flags={
        "record_kind": "decision_pass", "pass": "enter-market"})
    with use_capture_context(context):
        assert dr.decision_context() is None


def test_is_decision_pass_reads_the_flag_from_a_record_or_a_mapping():
    assert dr.is_decision_pass({"record_kind": "decision_pass"})
    assert not dr.is_decision_pass({}) and not dr.is_decision_pass({"record_kind": "analysis"})
    assert dr.is_decision_pass(SimpleNamespace(branch_flags={"record_kind": "decision_pass"}))


# --------------------------------------------------------------------------- rules
def test_the_rules_step_payload_shape():
    store = _Store()
    summaries = [{"action_type": "buy", "description": "Buy", "condition_evaluations": [1],
                  "rule_evaluations": [2]}]
    details = {"condition_evaluations": [{"condition_result": True}], "rule_evaluations": [{"executed": True}],
               "summary": {"total_rules": 1}}
    order = TradingOrder(id=5, account_id=3, symbol="AAPL", quantity=1.0, side=OrderDirection.BUY,
                         order_type=OrderType.MARKET, status=OrderStatus.FILLED)
    with capture_scope(store, _meta()) as context:
        with dr.record_rules(_Evaluator(details), "AAPL", _rec(), 9, order) as step:
            step.result = summaries
        context.set_skip("t")
    ((obs, payload),) = _steps(store, "rules")
    assert payload["version"] == dr.RECORD_VERSION == 1
    assert set(payload) == {"version", "tape_span", "inputs", "db_rows", "outcome"}
    assert obs.observation.request_identity["kind"] == "rules"
    assert obs.observation.request_identity["symbol"] == "AAPL"
    inputs = payload["inputs"]
    assert inputs["ruleset_id"] == 9 and inputs["pass"] == "enter-market"
    assert inputs["recommendation"]["symbol"] == "AAPL" and inputs["existing_order"]["id"] == 5
    assert inputs["existing_transaction_ids"] == [11, 12] and inputs["account_class"] == "SimpleNamespace"
    out = payload["outcome"]
    assert out["status"] == "returned" and out["error"] is None
    assert out["actions"] == [{"action_type": "buy", "description": "Buy"}]     # copies dropped
    assert out["evaluation_details"] == details
    assert payload["tape_span"].keys() == {"start", "clock_start", "clock_end"}
    assert store.health.total == 0


def test_a_rules_error_is_recorded_and_propagates_unchanged():
    store = _Store()
    with capture_scope(store, _meta()) as context:
        with pytest.raises(ValueError, match="bad ruleset"):
            with dr.record_rules(_Evaluator(), "AAPL", _rec(), 9, None):
                raise ValueError("bad ruleset")
        context.set_skip("t")
    ((_, payload),) = _steps(store, "rules")
    assert payload["outcome"]["status"] == "error" and "bad ruleset" in payload["outcome"]["error"]
    assert payload["outcome"]["evaluation_details"] is None


def test_an_unencodable_value_becomes_a_visible_marker_not_a_lost_record(monkeypatch):
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda *a, **k: None)
    store = _Store()
    details = {"condition_evaluations": [{"value": object()}], "rule_evaluations": []}
    with capture_scope(store, _meta()) as context:
        with dr.record_rules(_Evaluator(details), "AAPL", _rec(), 9, None) as step:
            step.result = []
        context.set_skip("t")
    ((_, payload),) = _steps(store, "rules")
    assert "unencodable" in payload["outcome"]["evaluation_details"]
    assert store.health.count(CaptureHealth.UNSUPPORTED_TYPE) + store.health.count(CaptureHealth.OTHER) >= 1


def test_a_recorder_that_cannot_build_its_inputs_is_counted_listed_and_harmless(monkeypatch):
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda *a, **k: None)
    store = _Store()
    broken = _Evaluator()
    broken.existing_transactions = None                      # the recorder's list comprehension raises
    with capture_scope(store, _meta()) as context:
        with dr.record_rules(broken, "AAPL", _rec(), 9, None) as step:
            step.result = ["the decision still proceeds"]
        summary = dr.pass_summary(context)
        context.set_skip("t")
    assert _steps(store, "rules") == []
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) == 1
    assert summary["steps_not_recorded"][0]["kind"] == "rules"
    assert summary["steps_not_recorded"][0]["symbol"] == "AAPL"


# --------------------------------------------------------------------------- execute + effects
def test_the_execute_step_collects_account_effects_and_the_rows_it_created():
    store = _Store()
    txn = add_instance(Transaction(symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
                                   status=TransactionStatus.OPENED, open_price=100.0, expert_id=7,
                                   stop_loss=95.0, take_profit=110.0))
    oid = add_instance(TradingOrder(account_id=1, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
                                    order_type=OrderType.MARKET, status=OrderStatus.NEW, transaction_id=txn,
                                    broker_order_id="B1"))
    results = [{"action_type": "buy", "success": True, "message": "ok",
                "data": {"order_id": oid, "evaluation_details": {"bulky": 1}}}]
    with capture_scope(store, _meta()) as context:
        dr.note_funded("AAPL", 10.0, 95.0)
        with dr.record_execute(_Evaluator(), "AAPL", _rec(), True) as step:
            assert note_account_effect("submit_order", call={"sl_price": 95.0}, returned="ok")
            step.result = results
        context.set_skip("t")
    ((_, payload),) = _steps(store, "execute")
    out = payload["outcome"]
    assert [e["method"] for e in out["effects"]] == ["submit_order"]
    assert out["results"][0]["data"] == {"order_id": oid}                 # bulky details dropped
    assert len(out["orders"]) == 1 and "id" not in out["orders"][0] and "broker_order_id" not in out["orders"][0]
    assert out["transactions"][0]["stop_loss"] == 95.0 and out["transactions"][0]["take_profit"] == 110.0
    assert {r["model"] for r in out["rows_after"]} == {"TradingOrder", "Transaction"}
    assert payload["inputs"]["funded"] == {"quantity": 10.0, "stop_price": 95.0}


def test_option_actions_are_left_to_the_option_action_record():
    from ba2_common.core.types import ExpertActionType

    with capture_scope(_Store(), _meta()):
        evaluator = _Evaluator()
        evaluator.trade_actions = [ExpertActionType.BUY_CALL]
        assert dr.record_execute(evaluator, "AAPL", _rec(), True) is dr._NOOP_CONTEXT
        evaluator.trade_actions = [ExpertActionType.BUY]
        assert dr.record_execute(evaluator, "AAPL", _rec(), True) is not dr._NOOP_CONTEXT


def test_only_the_execute_step_accepts_account_effects():
    store = _Store()
    with capture_scope(store, _meta()) as context:
        with dr.record_rules(_Evaluator(), "AAPL", _rec(), 9, None) as step:
            assert note_account_effect("adjust_sl", call={}) is False
            step.result = []
        context.set_skip("t")


# --------------------------------------------------------------------------- size
def test_the_size_step_carries_the_rms_own_explanation():
    store = _Store()
    candidate = TradingOrder(account_id=3, symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
                             order_type=OrderType.MARKET)
    manager = SimpleNamespace(indicator_provider=None, as_of=None)
    run = {"decisions": [{"symbol": "AAPL", "outcome": "FUNDED", "reason": "r", "quantity": 5.0}],
           "context": {"virtual_balance": 1000.0}}
    with capture_scope(store, _meta()) as context:
        with dr.record_size(manager, 7, [(candidate, _rec())], None) as step:
            assert dr.sizing_frame_open()
            dr.note_sizing_run(run["decisions"], run["context"])
            candidate.quantity = 5.0
            candidate.stop_price = 95.0
            step.result = [candidate]
        assert not dr.sizing_frame_open()
        summary = dr.pass_summary(context)
        context.set_skip("t")
    ((_, payload),) = _steps(store, "size")
    out = payload["outcome"]
    assert out["funded"] == [{"symbol": "AAPL", "side": OrderDirection.BUY, "quantity": 5.0,
                              "stop_price": 95.0}]
    assert out["decisions"] == run["decisions"] and out["context"] == run["context"]
    assert payload["inputs"]["candidates"][0]["order"]["symbol"] == "AAPL"
    assert summary["symbols"]["AAPL"]["size"] == {"outcome": "FUNDED", "reason": "r", "quantity": 5.0}


# --------------------------------------------------------------------------- pass_state + secrets
def test_pass_state_holds_the_expert_rulesets_and_book_and_drops_credentials():
    from ba2_common.core.models import EventAction, Ruleset, RulesetEventActionLink

    rs = add_instance(Ruleset(name="enter"))
    ea = add_instance(EventAction(name="r", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                                  triggers={}, actions={}))
    from ba2_common.core.db import get_db
    with get_db() as session:
        session.add(RulesetEventActionLink(ruleset_id=rs, eventaction_id=ea, order_index=0))
        session.commit()
    inst_id = add_instance(ExpertInstance(account_id=1, expert="FMPRating", enter_market_ruleset_id=rs))
    add_instance(ExpertSetting(instance_id=inst_id, key="data_provider_api_key", value_str="sk-LEAK"))
    add_instance(ExpertSetting(instance_id=inst_id, key="enable_buy", value_str="true"))
    add_instance(Transaction(symbol="MSFT", quantity=1.0, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, expert_id=inst_id))
    add_instance(Transaction(symbol="OLD", quantity=1.0, side=OrderDirection.BUY,
                             status=TransactionStatus.CLOSED, expert_id=inst_id))
    from ba2_common.core.db import get_instance

    instance = get_instance(ExpertInstance, inst_id)

    # the real filter is applied by full_recorded_settings; model it the same way here
    class _RealFilterExpert:
        def _recorded_settings(self, passed, flags):
            from ba2_common.core.replay.settings_record import full_recorded_settings
            return full_recorded_settings(
                {"enable_buy": True, "data_provider_api_key": "sk-LEAK"}, instance, passed)

    store = _Store()
    fetched = [_rec("AAPL", 1), _rec("MSFT", 2)]
    with capture_scope(store, _meta(expert_instance_id=inst_id)) as context:
        observation_id = dr.record_pass_state(
            expert=_RealFilterExpert(), expert_instance=instance, pass_name="enter-market",
            lookback_days=1, cutoff_time=UTC_NOW, decision_time=UTC_NOW,
            account=SimpleNamespace(id=1, OPTION_GREEKS_SOURCE=None), ruleset_id=rs,
            fetched_recommendations=fetched, selected_recommendations=fetched[:1])
        context.set_skip("t")
    assert observation_id is not None
    ((_, payload),) = _steps(store, "pass_state")
    inputs = payload["inputs"]
    assert "data_provider_api_key" in inputs["redacted_setting_keys"]
    assert "data_provider_api_key" not in inputs["settings"] and inputs["settings"]["enable_buy"] is True
    assert "sk-LEAK" not in repr(payload)
    models = [r["model"] for r in payload["db_rows"]]
    assert {"ExpertInstance", "ExpertSetting", "Ruleset", "EventAction", "RulesetEventActionLink",
            "ExpertRecommendation", "Transaction"} <= set(models)
    symbols = [r["row"]["symbol"] for r in payload["db_rows"] if r["model"] == "Transaction"]
    assert symbols == ["MSFT"]                                      # closed history is not recorded
    assert inputs["recommendations_selected"] == [1]
    assert [r["symbol"] for r in inputs["recommendations_considered"]] == ["AAPL", "MSFT"]
    assert inputs["cutoff_time"] == UTC_NOW and inputs["decision_time"] == UTC_NOW
    assert inputs["account_class"] == "SimpleNamespace"
    assert "AccountDefinition" not in models and "AccountSetting" not in models


def test_a_pass_state_failure_is_counted_and_listed_not_raised(monkeypatch):
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda *a, **k: None)
    store = _Store()
    with capture_scope(store, _meta()) as context:
        result = dr.record_pass_state(
            expert=None, expert_instance=SimpleNamespace(id=1), pass_name="enter-market",
            lookback_days=1, cutoff_time=None, decision_time=None, account=None, ruleset_id=None,
            fetched_recommendations=[], selected_recommendations=[])
        summary = dr.pass_summary(context)
        context.set_skip("t")
    assert result is None
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) == 1
    assert summary["steps_not_recorded"][0]["kind"] == "pass_state"


# --------------------------------------------------------------------------- views + summary
def test_the_views_are_pure_projections():
    candidate = SimpleNamespace(symbol="A", side="BUY", quantity=2.0, stop_price=None)
    assert dr.sizing_view([candidate], {"decisions": ["d"], "context": {"c": 1}}) == {
        "error": None, "funded": [{"symbol": "A", "side": "BUY", "quantity": 2.0, "stop_price": None}],
        "decisions": ["d"], "context": {"c": 1}}
    assert dr.sizing_view([], None, "boom")["decisions"] is None
    rows = [{"model": "TradingOrder", "row": {"id": 1, "created_at": 1, "side": "BUY", "quantity": 3.0,
                                                "broker_order_id": "x", "stop_price": 9.0}},
            {"model": "Transaction", "row": {"id": 2, "open_date": 1, "take_profit": 5.0, "stop_loss": 4.0}}]
    view = dr.execute_view([{"action_type": "buy", "success": True, "message": "m", "data": {"a": 1}}],
                           [{"method": "submit_order"}], rows)
    assert view["orders"] == [{"side": "BUY", "quantity": 3.0, "stop_price": 9.0}]
    assert view["transactions"] == [{"take_profit": 5.0, "stop_loss": 4.0}]
    assert view["effects"] == [{"method": "submit_order"}] and view["results"][0]["data"] == {"a": 1}
    assert dr.rules_view(None, {"x": 1}) == {"error": None, "actions": [], "evaluation_details": {"x": 1}}


def test_the_pass_summary_lists_gates_errors_and_funded_entries():
    with capture_scope(_Store(), _meta()) as context:
        dr.note_selection([_rec("AAPL", 1)])
        dr.note_gate("AAPL", "sufficient_equity", False, "insufficient")
        dr.note_funded("AAPL", 3.0, 90.0)
        dr.note_pass_error("recommendation 1", RuntimeError("x"))
        summary = dr.pass_summary(context, created=[4])
        context.set_decision(summary)
    assert summary["pass"] == "enter-market" and summary["expert_instance_id"] == 7
    assert summary["recommendations_considered"] == [{"id": 1, "symbol": "AAPL"}]
    assert summary["symbols"]["AAPL"]["gates"] == [
        {"name": "sufficient_equity", "passed": False, "detail": "insufficient"}]
    assert summary["funded"] == [{"symbol": "AAPL", "quantity": 3.0, "stop_price": 90.0}]
    assert summary["errors"] == ["recommendation 1: RuntimeError: x"] and summary["created_order_ids"] == [4]
