"""THE SAFETY PROOF for recording a live decision pass: capture ON changes nothing about the decision.

The REAL ``process_expert_recommendations_after_analysis`` (enter) and
``process_open_positions_recommendations`` (exit) run twice over the same seeded world, once with no
capture store and once with a real ``ReplayStore``. Everything a pass leaves behind must be IDENTICAL:
the returned orders, every TradingOrder / Transaction / TradeActionResult row (timestamps aside), every
call the fake account received (submit_order with its stop price, adjust_sl, ...). The capture-on run
must also END ``decision`` (not ``error``), with the steps the design promises and no capture failure.

Classic risk manager. The fake account inherits the REAL ``AccountInterface.submit_order`` (see
test_funded_entry_loop), so the order/transaction creation and the account effect taps run for real.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlmodel import SQLModel, select

import ba2_common.core.replay.decision_record as dr
from ba2_common.core.db import get_db
from ba2_common.core.models import TradeActionResult, TradingOrder, Transaction
from ba2_common.core.replay import (
    ReplayStatus, ReplayStore, SessionRecord, set_replay_store,
)
from ba2_common.core.replay.context import CaptureHealth
from ba2_common.core.types import (
    AnalysisUseCase, ExpertActionType, OrderDirection, OrderRecommendation, OrderStatus,
    TransactionStatus,
)
from tests import factories
from tests.test_funded_entry_loop import (
    FROZEN_NOW, _EnterExpert, _FundedEntryAccount, _make_scenario, _resolver_for, _run_enter,
)

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- helpers
class _Account(_FundedEntryAccount):
    """The funded-entry fake with the REAL quote signature (``symbol_or_symbols``): the quote tap binds the
    argument by that name, as it does on every shipped account."""

    def get_instrument_current_price(self, symbol_or_symbols, price_type="bid"):
        if isinstance(symbol_or_symbols, (list, tuple, set)):
            return {s: self._prices.get(s, 100.0) for s in symbol_or_symbols}
        return self._prices.get(symbol_or_symbols, 100.0)


def _reset(test_engine):
    SQLModel.metadata.drop_all(test_engine)
    SQLModel.metadata.create_all(test_engine)


def _normal(row):
    """A row as a comparable dict: timestamps dropped (wall clock), everything else verbatim."""
    out = {k: v for k, v in row.model_dump().items()
           if not (k.endswith("_at") or k.endswith("_date") or k == "last_updated")}
    return out


def _snapshot():
    with get_db() as session:
        return {
            "orders": [_normal(o) for o in session.exec(select(TradingOrder).order_by(TradingOrder.id)).all()],
            "transactions": [_normal(t) for t in session.exec(select(Transaction).order_by(Transaction.id)).all()],
            "results": [_normal(r) for r in session.exec(select(TradeActionResult).order_by(TradeActionResult.id)).all()],
        }


@pytest.fixture
def make_store(tmp_path):
    made = []

    def _make():
        store = ReplayStore(tmp_path / f"store{len(made)}", writer="sync")
        store.begin_session(SessionRecord(
            session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
            app_version="x", package_versions={}, source_revision="abc", dirty=False))
        set_replay_store(store)
        made.append(store)
        return store

    yield _make
    set_replay_store(None)
    for store in made:
        store.close(timeout=2.0)


def _steps(store, kind=None):
    (analysis,) = store.index.analyses("s1")
    out = []
    for observation in store.index.observations(analysis.analysis_id):
        if observation.provider != dr.DECISION_PROVIDER:
            continue
        if kind is None or observation.method == kind:
            out.append((observation, store.decode_object(observation.payload_object)))
    return analysis, out


# --------------------------------------------------------------------------- the ENTER pass
def _enter_world(account_cls_kwargs=None):
    acct_def, inst, expert = _make_scenario(["AAPL", "MSFT", "GOOGL"], with_stop_loss=True,
                                            per_instrument_pct=40.0)
    account = _Account(acct_def.id, **(account_cls_kwargs or {}))
    return inst, expert, account


def _run_enter_pass(test_engine, store=None):
    _reset(test_engine)
    inst, expert, account = _enter_world()
    created = _run_enter(expert, account, inst.id)
    return {
        "created": [o.id for o in created],
        "submits": account.submits,
        **_snapshot(),
    }, inst


def test_the_enter_pass_is_identical_with_capture_on_and_off(test_engine, make_store):
    off, _ = _run_enter_pass(test_engine)
    assert off["created"], "the scenario must actually fund and submit something"
    store = make_store()
    on, inst = _run_enter_pass(test_engine, store)

    assert on == off                                             # orders, transactions, results, submits

    analysis, _ = _steps(store)
    assert analysis.outcome == ReplayStatus.OUTCOME_DECISION, analysis.error
    assert analysis.error is None and analysis.capture_failures == {}
    assert store.health.total == 0
    assert analysis.expert_class == "_EnterExpert" and analysis.branch_flags["pass"] == "enter-market"
    summary = store.decode_object(analysis.decision_object)
    assert summary["created_order_ids"] == off["created"]
    assert summary["errors"] == [] and summary["steps_not_recorded"] == []


def test_the_enter_pass_records_pass_state_rules_size_and_execute_steps(test_engine, make_store):
    store = make_store()
    off_len = None
    on, inst = _run_enter_pass(test_engine, store)
    analysis, steps = _steps(store)
    kinds = [o.method for o, _ in steps]
    assert kinds.count("pass_state") == 1 and kinds.count("rules") == 3
    assert kinds.count("size") == 1 and kinds.count("execute") == len(on["created"]) >= 1
    assert kinds[0] == "pass_state"

    payloads = {o.method: p for o, p in reversed(steps)}
    state = payloads["pass_state"]["inputs"]
    assert state["pass"] == "enter-market" and state["expert_class"] == "_EnterExpert"
    assert len(state["recommendations_selected"]) == 3
    models = {row["model"] for row in payloads["pass_state"]["db_rows"]}
    assert {"ExpertInstance", "ExpertSetting", "Ruleset", "EventAction", "RulesetEventActionLink",
            "ExpertRecommendation"} <= models

    execute_payloads = [p for o, p in steps if o.method == "execute"]
    for payload in execute_payloads:
        methods = [e["method"] for e in payload["outcome"]["effects"]]
        assert "submit_order" in methods                      # the effect tap recorded the submit
        assert payload["inputs"]["funded"]["quantity"] > 0
        assert payload["outcome"]["orders"] and payload["outcome"]["transactions"]
        submit = next(e for e in payload["outcome"]["effects"] if e["method"] == "submit_order")
        assert submit["call"]["sl_price"] is not None                # the final stop handed to the broker

    size = next(p for o, p in steps if o.method == "size")
    assert {row["symbol"] for row in size["outcome"]["decisions"]} == {"AAPL", "MSFT", "GOOGL"}
    assert size["outcome"]["context"] is not None
    funded = {f["symbol"] for f in size["outcome"]["funded"]}
    assert funded == {p["inputs"]["instrument_name"] for p in execute_payloads}

    rules = [p for o, p in steps if o.method == "rules"]
    assert all(p["outcome"]["evaluation_details"] for p in rules)
    assert all(p["inputs"]["recommendation"]["symbol"] in {"AAPL", "MSFT", "GOOGL"} for p in rules)

    summary = store.decode_object(analysis.decision_object)
    assert {g["name"] for g in summary["symbols"]["AAPL"]["gates"]} == {
        "open_or_waiting_position", "sufficient_equity", "entry_route"}


def test_a_secret_setting_is_absent_from_the_recorded_pass_state_and_named(test_engine, make_store):
    _reset(test_engine)
    store = make_store()
    inst, expert, account = _enter_world()
    expert.save_settings({"data_provider_api_key": ("sk-LEAK-ME", "str")})
    _run_enter(expert, account, inst.id)
    _, steps = _steps(store, "pass_state")
    ((_, payload),) = steps
    assert "data_provider_api_key" in payload["inputs"]["redacted_setting_keys"]
    assert "data_provider_api_key" not in payload["inputs"]["settings"]
    assert all(row["row"].get("key") != "data_provider_api_key" for row in payload["db_rows"])
    assert "sk-LEAK-ME" not in repr(payload)


def test_a_broken_recorder_never_changes_the_enter_pass(test_engine, make_store, monkeypatch):
    """Every step recorder raises: the pass still funds and submits exactly what it did without capture."""
    off, _ = _run_enter_pass(test_engine)
    store = make_store()

    def _boom(self, *a, **k):
        raise RuntimeError("recorder defect")

    for name in ("build_inputs",):
        for cls in (dr._RulesRecorder, dr._SizeRecorder, dr._ExecuteRecorder):
            monkeypatch.setattr(cls, name, _boom)
    monkeypatch.setattr(dr, "_pass_state_rows", _boom)
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda *a, **k: None)
    on, _ = _run_enter_pass(test_engine, store)
    assert on == off
    analysis, _ = _steps(store)
    assert analysis.outcome == ReplayStatus.OUTCOME_DECISION
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) >= 5
    summary = store.decode_object(analysis.decision_object)
    kinds = {s["kind"] for s in summary["steps_not_recorded"]}
    assert kinds == {"pass_state", "rules", "size", "execute"}


def test_a_failing_submit_is_identical_and_recorded_as_an_error_effect(test_engine, make_store):
    def run(store):
        _reset(test_engine)
        inst, expert, account = _enter_world({"fail_symbols": {"AAPL"}, "fail_exc": RuntimeError("refused")})
        created = _run_enter(expert, account, inst.id)
        return {"created": [o.id for o in created], **_snapshot()}

    off = run(None)
    store = make_store()
    on = run(store)
    assert on == off
    _, steps = _steps(store, "execute")
    errored = [p for o, p in steps if p["inputs"]["instrument_name"] == "AAPL"]
    assert errored and any(e["method"] == "submit_order" for p in errored for e in p["outcome"]["effects"])


# --------------------------------------------------------------------------- the EXIT pass
def _exit_world(action=None):
    acct_def = factories.create_account_definition(provider="MockAccount")
    rs = factories.create_ruleset(name="exits", subtype=AnalysisUseCase.OPEN_POSITIONS)
    rule = factories.create_event_action(
        name="trail", subtype=AnalysisUseCase.OPEN_POSITIONS,
        triggers={"trigger_0": {"event_type": "confidence", "operator": ">=", "value": 60.0}},
        actions={"action_0": action or {"action_type": ExpertActionType.ADJUST_STOP_LOSS.value,
                                        "value": -5.0, "reference_value": "current_price"}})
    factories.link_rule_to_ruleset(rs.id, rule.id, 0)
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_EnterExpert", open_positions_ruleset_id=rs.id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({"allow_automated_trade_modification": (True, "bool")})
    factories.create_recommendation(
        instance_id=inst.id, symbol="AAPL", recommended_action=OrderRecommendation.HOLD,
        expected_profit_percent=5.0, price_at_date=100.0, confidence=90.0,
        subtype=AnalysisUseCase.OPEN_POSITIONS, created_at=FROZEN_NOW)
    txn = factories.create_transaction(symbol="AAPL", quantity=10.0, status=TransactionStatus.OPENED,
                                       open_price=100.0, expert_id=inst.id)
    factories.create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                                   status=OrderStatus.FILLED, transaction_id=txn.id,
                                   side=OrderDirection.BUY, open_price=100.0)
    account = _Account(acct_def.id)
    return inst, expert, account


def _run_exit(expert, account, expert_instance_id):
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver
    from ba2_trade_platform.core import TradeManager as _tm_mod
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    try:
        previous = get_instance_resolver()
    except Exception:  # noqa: BLE001
        previous = None
    set_instance_resolver(_resolver_for(expert, account))
    try:
        with patch.object(_tm_mod, "replay_now", lambda *a, **k: FROZEN_NOW), \
             patch("ba2_trade_platform.core.utils.get_expert_instance_from_id", return_value=expert), \
             patch("ba2_trade_platform.modules.accounts.get_account_class",
                   return_value=(lambda _id: account)):
            return get_trade_manager().process_open_positions_recommendations(
                expert_instance_id, lookback_days=7)
    finally:
        if previous is not None:
            set_instance_resolver(previous)


def _run_exit_pass(test_engine, action=None):
    _reset(test_engine)
    inst, expert, account = _exit_world(action)
    results = _run_exit(expert, account, inst.id)
    return {"results": [{k: r[k] for k in ("action_type", "success", "message")} for r in results],
            "submits": account.submits, **_snapshot()}


def test_the_exit_pass_is_identical_with_capture_on_and_off(test_engine, make_store):
    off = _run_exit_pass(test_engine)
    assert off["results"] and all(r["success"] for r in off["results"]), off["results"]
    store = make_store()
    on = _run_exit_pass(test_engine)

    assert on == off

    analysis, steps = _steps(store)
    assert analysis.outcome == ReplayStatus.OUTCOME_DECISION, analysis.error
    assert analysis.capture_failures == {} and store.health.total == 0
    assert analysis.branch_flags["pass"] == "open-positions"
    kinds = [o.method for o, _ in steps]
    assert kinds.count("pass_state") == 1 and kinds.count("rules") == 1 and kinds.count("execute") == 1
    assert kinds.count("size") == 0                       # exits have no RM sizing
    execute = next(p for o, p in steps if o.method == "execute")
    effects = [e["method"] for e in execute["outcome"]["effects"]]
    assert any(m in ("adjust_sl", "adjust_tp_sl") for m in effects), effects
    assert execute["inputs"]["submit_to_broker"] is True
    assert execute["inputs"]["existing_transaction_ids"]
    rules = next(p for o, p in steps if o.method == "rules")
    assert rules["inputs"]["existing_order"] is not None
    assert [a["action_type"] for a in rules["outcome"]["actions"]]


def test_a_broken_recorder_never_changes_the_exit_pass(test_engine, make_store, monkeypatch):
    off = _run_exit_pass(test_engine)
    store = make_store()

    def _boom(self, *a, **k):
        raise RuntimeError("recorder defect")

    for cls in (dr._RulesRecorder, dr._ExecuteRecorder):
        monkeypatch.setattr(cls, "build_inputs", _boom)
    monkeypatch.setattr(dr, "_pass_state_rows", _boom)
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda *a, **k: None)
    assert _run_exit_pass(test_engine) == off
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) >= 3


def test_a_closing_exit_is_identical_and_records_the_close_effect(test_engine, make_store):
    close = {"action_type": ExpertActionType.CLOSE.value}
    off = _run_exit_pass(test_engine, close)
    assert off["results"] and all(r["success"] for r in off["results"]), off["results"]
    store = make_store()
    on = _run_exit_pass(test_engine, close)
    assert on == off
    analysis, steps = _steps(store)
    assert analysis.outcome == ReplayStatus.OUTCOME_DECISION and analysis.capture_failures == {}
    execute = next(p for o, p in steps if o.method == "execute")
    effects = [e["method"] for e in execute["outcome"]["effects"]]
    assert effects and effects[0] in ("close_transaction", "submit_order"), effects
