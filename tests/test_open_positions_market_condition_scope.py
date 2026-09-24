"""The live OPEN_POSITIONS pass opens the market-condition decision scope (plan B1).

Before this change only the ENTRY pass (``process_expert_recommendations_after_analysis``)
opened ``market_condition_decision_scope``. A market-condition leaf in an EXIT rule therefore
read ``no_context`` live and never fired, while the backtest resolver evaluates it: a BT/live
parity break.

These tests drive the REAL ``TradeManager.process_open_positions_recommendations`` against the
in-memory test DB (expert instance, recommendation, open transaction). Only the edges are
faked: the expert object (its ``allow_automated_trade_modification`` setting), the account
class, and ``TradeActionEvaluator``, whose ``evaluate`` records what the decision scope looked
like at the moment the rules were evaluated.

Pinned:

* the scope opens exactly ONCE per pass, for THIS expert, and the rules evaluate INSIDE it;
* it is not opened at all on the early returns (trade modification off, no open_positions
  ruleset), so those experts never even read their profile;
* NO-OP: for an expert with no ``market_condition_profile`` the real scope yields None, reads no
  clock, and the pass returns exactly what it returns with no scope at all -- which is every
  live expert today;
* POSITIVE: for an expert WITH a profile, a real ``MarketConditionCompare`` leaf evaluated during
  the pass gets a context and a valid observation; the same leaf without the scope reads
  ``no_context`` (the parity break this closes).
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_conditions import STATUS_NO_CONTEXT, STATUS_VALID
from ba2_common.core.types import ExpertEventType

from ba2_trade_platform.core.types import OrderRecommendation, TransactionStatus
from tests.factories import (
    create_account_definition,
    create_expert_instance,
    create_recommendation,
    create_ruleset,
    create_transaction,
)
# The stub instance-resolver / dispatcher / clock fixtures of the live-context suite, reused so
# the per-instance resolver here is exactly the one ``wire_all_seams`` installs.
from tests.test_market_condition_live_context import (  # noqa: F401 -- pytest fixtures
    DECISION,
    cache_root,
    clock,
    dispatcher,
    instances,
)

SYMBOL = "AAA"   # the symbol the reused ``cache_root`` fixture writes a parquet for


# --------------------------------------------------------------------------- fakes
class _FakeExpert:
    def __init__(self, allow_modification=True):
        self._allow = allow_modification

    def get_setting_with_interface_default(self, key, log_warning=True):
        assert key == "allow_automated_trade_modification"
        return self._allow


class _FakeAccount:
    def __init__(self, account_id):
        self.id = account_id

    def has_pending_closing_order(self, transaction_id):
        return False


class _Recorder:
    """What the fake evaluator saw, one entry per ``evaluate`` call."""

    def __init__(self):
        self.calls = []
        self.leaf = None          # optional callable(account, symbol, rec) -> extra record


def _fake_evaluator_class(recorder: _Recorder, active_scope):
    """A ``TradeActionEvaluator`` stand-in. ``active_scope()`` reports the recorder scope's
    state (for the monkeypatched-scope test); ``live.current_decision()`` is the real contextvar."""

    class _FakeEvaluator:
        def __init__(self, account, instrument_name, existing_transactions):
            self.account = account
            self.instrument_name = instrument_name
            self.existing_transactions = existing_transactions

        def evaluate(self, instrument_name, expert_recommendation, ruleset_id, existing_order=None):
            entry = {
                "symbol": instrument_name,
                "recommendation_id": expert_recommendation.id,
                "ruleset_id": ruleset_id,
                "transaction_ids": sorted(t.id for t in self.existing_transactions),
                "scope_active": active_scope(),
                "decision": live.current_decision(),
            }
            if recorder.leaf is not None:
                entry["leaf"] = recorder.leaf(self.account, instrument_name, expert_recommendation)
            recorder.calls.append(entry)
            return [{"action": "close"}]

        def get_evaluation_details(self):
            return {}

        def execute(self, submit_to_broker=False):
            return [{"success": True, "symbol": self.instrument_name,
                     "submit_to_broker": submit_to_broker}]

    return _FakeEvaluator


@pytest.fixture
def world(monkeypatch):
    """An expert with an open_positions ruleset, one OPEN_POSITIONS recommendation and one open
    transaction on ``SYMBOL``, in the in-memory DB; the account/expert/evaluator edges faked."""
    import ba2_common.core.TradeActionEvaluator as tae_mod
    import ba2_trade_platform.core.utils as core_utils
    import ba2_trade_platform.modules.accounts as accounts_mod
    from ba2_trade_platform.core.types import AnalysisUseCase

    account_def = create_account_definition()
    ruleset = create_ruleset(name="exits")
    expert_instance = create_expert_instance(account_id=account_def.id,
                                             open_positions_ruleset_id=ruleset.id)
    rec = create_recommendation(instance_id=expert_instance.id, symbol=SYMBOL,
                                recommended_action=OrderRecommendation.SELL,
                                subtype=AnalysisUseCase.OPEN_POSITIONS)
    txn = create_transaction(symbol=SYMBOL, status=TransactionStatus.OPENED,
                             expert_id=expert_instance.id)

    state = {"expert": _FakeExpert(), "scope_depth": 0}
    recorder = _Recorder()

    monkeypatch.setattr(core_utils, "get_expert_instance_from_id",
                        lambda expert_id, use_cache=True: state["expert"])
    monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: _FakeAccount)
    monkeypatch.setattr(tae_mod, "TradeActionEvaluator",
                        _fake_evaluator_class(recorder, lambda: state["scope_depth"] > 0))

    return {
        "expert_id": expert_instance.id,
        "ruleset_id": ruleset.id,
        "recommendation_id": rec.id,
        "transaction_id": txn.id,
        "state": state,
        "recorder": recorder,
    }


def _run(expert_id):
    from ba2_trade_platform.core.TradeManager import TradeManager

    return TradeManager().process_open_positions_recommendations(expert_id)


def _recording_scope(world, opened):
    """A stand-in for ``market_condition_decision_scope`` that records each opening and marks
    the world while it is open."""

    @contextmanager
    def _scope(*, expert_instance_id=None, replay_reader=None):
        opened.append(expert_instance_id)
        world["state"]["scope_depth"] += 1
        try:
            yield None
        finally:
            world["state"]["scope_depth"] -= 1

    return _scope


# --------------------------------------------------------------------------- the scope is opened
def test_the_pass_opens_the_scope_once_for_this_expert_and_evaluates_inside_it(world, monkeypatch):
    opened = []
    monkeypatch.setattr(live, "market_condition_decision_scope", _recording_scope(world, opened))

    result = _run(world["expert_id"])

    assert opened == [world["expert_id"]]
    calls = world["recorder"].calls
    assert len(calls) == 1
    assert calls[0]["symbol"] == SYMBOL
    assert calls[0]["recommendation_id"] == world["recommendation_id"]
    assert calls[0]["ruleset_id"] == world["ruleset_id"]
    assert calls[0]["scope_active"] is True           # the rules evaluated INSIDE the scope
    assert world["state"]["scope_depth"] == 0          # and it is closed again afterwards
    assert result == [{"success": True, "symbol": SYMBOL, "submit_to_broker": True}]


def test_an_expert_with_trade_modification_off_never_opens_the_scope(world, monkeypatch):
    opened = []
    monkeypatch.setattr(live, "market_condition_decision_scope", _recording_scope(world, opened))
    world["state"]["expert"] = _FakeExpert(allow_modification=False)

    assert _run(world["expert_id"]) == []
    assert opened == [] and world["recorder"].calls == []


def test_an_expert_with_no_open_positions_ruleset_never_opens_the_scope(world, monkeypatch):
    from ba2_trade_platform.core.db import get_instance, update_instance
    from ba2_trade_platform.core.models import ExpertInstance

    opened = []
    monkeypatch.setattr(live, "market_condition_decision_scope", _recording_scope(world, opened))
    inst = get_instance(ExpertInstance, world["expert_id"])
    inst.open_positions_ruleset_id = None
    update_instance(inst)

    assert _run(world["expert_id"]) == []
    assert opened == [] and world["recorder"].calls == []


def test_a_failed_lock_acquisition_never_opens_the_scope(world, monkeypatch):
    """The scope sits after the lock: a pass that skips because another thread holds the lock
    must not read the profile or the clock."""
    import threading

    from ba2_trade_platform.core.TradeManager import TradeManager

    opened = []
    monkeypatch.setattr(live, "market_condition_decision_scope", _recording_scope(world, opened))
    tm = TradeManager()
    held = threading.Lock()
    held.acquire()
    tm._processing_locks[f"expert_{world['expert_id']}_usecase_open_positions"] = held
    try:
        assert tm.process_open_positions_recommendations(world["expert_id"]) == []
    finally:
        held.release()
    assert opened == [] and world["recorder"].calls == []


# --------------------------------------------------------------------------- no-op proof
def test_no_profile_the_real_scope_is_a_strict_no_op(world, dispatcher, instances, clock,
                                                     monkeypatch):
    """Every live expert today: no ``market_condition_profile`` setting. The real scope, with the
    real per-instance dispatcher installed, yields None, reads no clock, sets no decision state,
    and the pass returns exactly what it returns with NO scope at all (the pre-change code)."""
    instances[world["expert_id"]] = ""
    asked = []
    real_profiles_for = dispatcher.profiles_for
    monkeypatch.setattr(dispatcher, "profiles_for",
                        lambda iid: asked.append(iid) or real_profiles_for(iid))

    with_scope = _run(world["expert_id"])
    calls_with_scope = list(world["recorder"].calls)

    # The real scope really ran and asked THIS expert's setting -- and answered "no profile".
    assert asked == [world["expert_id"]]
    assert live.resolver_for_expert_instance(world["expert_id"]) is None
    assert clock == []                                   # no clock read
    assert [c["decision"] for c in calls_with_scope] == [None]
    assert live.current_decision() is None

    # The same pass with the scope removed entirely: identical result, identical evaluation.
    @contextmanager
    def _no_scope(*, expert_instance_id=None, replay_reader=None):
        yield None

    world["recorder"].calls.clear()
    monkeypatch.setattr(live, "market_condition_decision_scope", _no_scope)
    without_scope = _run(world["expert_id"])

    assert with_scope == without_scope
    assert world["recorder"].calls == calls_with_scope
    assert clock == []


def test_no_profile_a_market_leaf_in_an_exit_rule_still_reads_no_context(world, dispatcher,
                                                                           instances, clock):
    """Nothing changes for an un-gated expert's market leaf either: FALSE, ``no_context``, with
    the reason naming the missing setting -- exactly as before the scope was opened here."""
    instances[world["expert_id"]] = ""
    world["recorder"].leaf = _adx_leaf

    _run(world["expert_id"])

    (call,) = world["recorder"].calls
    assert call["leaf"]["passed"] is False
    assert call["leaf"]["status"] == STATUS_NO_CONTEXT
    assert "market_condition_profile" in call["leaf"]["reason"]
    assert clock == []


# --------------------------------------------------------------------------- positive proof
def _adx_leaf(account, symbol, rec):
    """A real ``MarketConditionCompare`` leaf (an always-true threshold), evaluated in place."""
    leaf = TC.create_condition(ExpertEventType.N_UNDERLYING_ADX, account, symbol, rec,
                               operator_str=">", value=-1e9)
    ctx = TC.resolve_market_condition_context(account, symbol, rec)
    passed = leaf.evaluate()
    return {"passed": passed, "status": leaf.last_status, "reason": leaf.last_reason,
            "context": ctx}


def test_with_a_profile_a_market_leaf_evaluated_in_the_pass_gets_a_context(world, dispatcher,
                                                                         instances, clock):
    instances[world["expert_id"]] = "ohlcv-v1"
    world["recorder"].leaf = _adx_leaf

    _run(world["expert_id"])

    (call,) = world["recorder"].calls
    assert call["decision"] is not None and call["decision"].decision_time == DECISION
    assert call["leaf"]["context"] is not None
    assert call["leaf"]["status"] == STATUS_VALID
    assert call["leaf"]["passed"] is True
    # One pass, one clock read -- and nothing left open once the pass returns.
    assert len(clock) == 1
    assert live.current_decision() is None


def test_with_a_profile_but_no_scope_the_same_leaf_reads_no_context(world, dispatcher, instances,
                                                                   clock, monkeypatch):
    """The control: the pre-change pass (no scope) leaves a gated expert's exit leaf without a
    context -- the BT/live parity break the scope closes."""
    instances[world["expert_id"]] = "ohlcv-v1"
    world["recorder"].leaf = _adx_leaf

    @contextmanager
    def _no_scope(*, expert_instance_id=None, replay_reader=None):
        yield None

    monkeypatch.setattr(live, "market_condition_decision_scope", _no_scope)
    _run(world["expert_id"])

    (call,) = world["recorder"].calls
    assert call["decision"] is None
    assert call["leaf"]["context"] is None
    assert call["leaf"]["status"] == STATUS_NO_CONTEXT
    assert call["leaf"]["passed"] is False
    assert clock == []


# --------------------------------------------------------------------------- a failed opening
class _LogSpy:
    """Stands in for ``TradeManager.logger`` (the ba2 loggers do not propagate to caplog)."""

    def __init__(self, raise_on_info=None):
        self.records = []
        self._raise_on_info = raise_on_info

    def _add(self, level, msg, kwargs):
        self.records.append((level, str(msg), bool(kwargs.get("exc_info"))))

    def error(self, msg, *a, **k):
        self._add("ERROR", msg, k)

    def warning(self, msg, *a, **k):
        self._add("WARNING", msg, k)

    def info(self, msg, *a, **k):
        self._add("INFO", msg, k)
        if self._raise_on_info is not None and "unique instruments" in str(msg):
            raise self._raise_on_info

    def debug(self, msg, *a, **k):
        self._add("DEBUG", msg, k)

    def errors(self):
        return [r for r in self.records if r[0] == "ERROR"]


@pytest.fixture
def activities(monkeypatch):
    """Captures ``log_activity`` (asynchronous in production) where TradeManager imports it."""
    import ba2_trade_platform.core.db as core_db

    seen = []
    monkeypatch.setattr(core_db, "log_activity", lambda **kw: seen.append(kw))
    return seen


def _run_with(expert_id, spy):
    from ba2_trade_platform.core.TradeManager import TradeManager

    tm = TradeManager()
    tm.logger = spy
    return tm.process_open_positions_recommendations(expert_id)


def _raising_scope(exc):
    class _Scope:
        def __init__(self, *, expert_instance_id=None, replay_reader=None):
            pass

        def __enter__(self):
            raise exc

        def __exit__(self, *a):
            raise AssertionError("a scope that never opened must never be exited")

    return _Scope


def _no_scope_run(world, monkeypatch):
    @contextmanager
    def _no_scope(*, expert_instance_id=None, replay_reader=None):
        yield None

    monkeypatch.setattr(live, "market_condition_decision_scope", _no_scope)
    world["recorder"].calls.clear()
    result = _run(world["expert_id"])
    return result, list(world["recorder"].calls)


@pytest.mark.parametrize("exc", [
    ValueError("malformed BA2_MARKET_CONDITION_MANIFEST"),
    TypeError("resolver seam returned the wrong shape"),
    AttributeError("'NoneType' object has no attribute 'settings'"),
    RuntimeError("reader build failed"),
], ids=lambda e: type(e).__name__)
def test_a_scope_that_fails_to_open_does_not_stop_the_exit_pass(world, activities, monkeypatch,
                                                                exc):
    """Exits are never blocked by the market-condition machinery -- under the enforce error mode
    the platform runs with, whatever the scope raises."""
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    monkeypatch.setattr(live, "market_condition_decision_scope", _raising_scope(exc))
    spy = _LogSpy()

    result = _run_with(world["expert_id"], spy)
    calls = list(world["recorder"].calls)

    # The exit rules were still evaluated AND executed, with no decision state.
    assert len(calls) == 1 and calls[0]["decision"] is None
    assert result == [{"success": True, "symbol": SYMBOL, "submit_to_broker": True}]
    # ERROR, with the traceback, naming the expert and what it means for this pass.
    (err,) = spy.errors()
    assert err[2] is True
    assert f"Expert instance {world['expert_id']}" in err[1]
    assert "no_context this pass" in err[1] and type(exc).__name__ in err[1]
    # ...and where operators look: one FAILURE activity row for this expert.
    from ba2_trade_platform.core.types import ActivityLogSeverity, ActivityLogType

    (act,) = activities
    assert act["severity"] == ActivityLogSeverity.FAILURE
    assert act["activity_type"] == ActivityLogType.RISK_MANAGER_RAN
    assert act["source_expert_id"] == world["expert_id"]
    assert act["data"]["error_type"] == type(exc).__name__

    # Same return value and same evaluation as a pass with no scope at all.
    assert (result, calls) == _no_scope_run(world, monkeypatch)


def test_a_real_malformed_manifest_does_not_stop_the_exit_pass(world, dispatcher, instances,
                                                              clock, activities, monkeypatch):
    """The concrete case from the resolver's docstring, through the REAL scope and dispatcher: a
    gated expert with a malformed manifest env var raises when its resolver is built. The exit
    rules (here: no market leaf) are still evaluated and executed."""
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    instances[world["expert_id"]] = "ohlcv-v1"
    dispatcher._environ = {live.MANIFEST_ENV: "no-such-profile=abc"}
    spy = _LogSpy()

    result = _run_with(world["expert_id"], spy)

    (call,) = world["recorder"].calls
    assert call["decision"] is None
    assert result == [{"success": True, "symbol": SYMBOL, "submit_to_broker": True}]
    (err,) = spy.errors()
    assert "ValueError" in err[1] and "no_context this pass" in err[1]
    assert len(activities) == 1
    assert clock == [] and live.current_decision() is None


def test_a_market_leaf_after_a_failed_scope_reads_no_context(world, dispatcher, instances, clock,
                                                            monkeypatch):
    """Was the strict xfail pinning the gap: after the pass guard absorbed the scope failure, the
    leaf's own dispatch re-raised the malformed-manifest ValueError. With NO decision state open
    the dispatcher now answers "no context" whatever fails, with a reason saying so."""
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    instances[world["expert_id"]] = "ohlcv-v1"
    dispatcher._environ = {live.MANIFEST_ENV: "no-such-profile=abc"}
    got = _adx_leaf(object(), SYMBOL, _rec_for(world["expert_id"]))
    assert got["status"] == STATUS_NO_CONTEXT and got["passed"] is False
    assert got["reason"].startswith(f"expert instance {world['expert_id']} names a ")
    assert "no market-condition decision scope is open" in got["reason"]
    # I1: the CAUSE is in the reason, not only the fact that something failed.
    assert "ValueError" in got["reason"] and "no-such-profile" in got["reason"]
    assert clock == []


# ------------------------------------------- the dispatcher outside a decision scope (contract)
def _rec_for(instance_id):
    return type("Rec", (), {"instance_id": instance_id, "symbol": SYMBOL})()


@pytest.mark.parametrize("boom", [RuntimeError("reader build failed"), ValueError("bad manifest"),
                                  TypeError("seam defect")], ids=lambda e: type(e).__name__)
def test_no_scope_and_a_failing_build_reads_no_context_and_never_raises(dispatcher, instances,
                                                                       clock, monkeypatch, boom):
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    instances[5] = "ohlcv-v1"

    def _build(profiles):
        raise boom

    monkeypatch.setattr(dispatcher, "_build", _build)
    assert live.current_decision() is None
    assert dispatcher(object(), SYMBOL, _rec_for(5)) is None
    got = _adx_leaf(object(), SYMBOL, _rec_for(5))
    assert got["status"] == STATUS_NO_CONTEXT and got["passed"] is False
    assert got["reason"] == live.no_scope_open_reason(5, f"{type(boom).__name__}: {boom}")
    assert "no market-condition decision scope is open" in got["reason"]
    assert clock == []


def test_no_scope_and_a_broken_settings_read_reads_no_context_and_never_raises(dispatcher, clock,
                                                                              monkeypatch):
    """``profiles_for`` lets a TypeError/AttributeError from the instance resolver propagate (a
    defect must not read as "no profile"). Outside a scope that still must not raise here."""
    import ba2_common.core.instance_resolver as ir

    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")

    class _Broken:
        def get_expert_instance(self, expert_id):
            raise AttributeError("resolver seam returned the wrong shape")

    monkeypatch.setattr(ir, "get_instance_resolver", lambda: _Broken())
    got = _adx_leaf(object(), SYMBOL, _rec_for(6))
    assert got["status"] == STATUS_NO_CONTEXT and got["passed"] is False
    assert got["reason"] == live.no_scope_open_reason(
        6, "AttributeError: resolver seam returned the wrong shape")


def test_no_scope_and_a_healthy_profile_keeps_todays_reason(dispatcher, instances, clock):
    """Nothing failed, the caller just has no scope open: today's reason, unchanged."""
    instances[7] = "ohlcv-v1"
    got = _adx_leaf(object(), SYMBOL, _rec_for(7))
    assert got["status"] == STATUS_NO_CONTEXT and got["passed"] is False
    assert got["reason"] == live.NO_DECISION_SCOPE_REASON
    assert "OUTSIDE a market_condition_decision_scope" in got["reason"]


def test_no_scope_and_no_profile_keeps_the_empty_setting_reason(dispatcher, instances, clock):
    instances[8] = ""
    got = _adx_leaf(object(), SYMBOL, _rec_for(8))
    assert got["status"] == STATUS_NO_CONTEXT and got["passed"] is False
    assert "expert instance 8 has an empty market_condition_profile setting" in got["reason"]


def test_inside_an_open_scope_a_build_error_still_propagates(dispatcher, instances, clock,
                                                           monkeypatch):
    """ENTRY-PATH SEMANTICS UNCHANGED: with a decision state open, a resolver build error raises
    out of the leaf exactly as before (here: expert 1's pass is open, a leaf for expert 2 whose
    resolver cannot be built)."""
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    instances[1] = "ohlcv-v1"
    instances[2] = "ta-structure-v1"
    real_build = dispatcher._build

    def _build(profiles):
        if profiles == ("ta-structure-v1",):
            raise RuntimeError("reader build failed")
        return real_build(profiles)

    monkeypatch.setattr(dispatcher, "_build", _build)
    with live.market_condition_decision_scope(expert_instance_id=1) as state:
        assert state is not None
        assert _adx_leaf(object(), SYMBOL, _rec_for(1))["status"] == STATUS_VALID
        leaf = TC.create_condition(ExpertEventType.N_UNDERLYING_ADX, object(), SYMBOL,
                                   _rec_for(2), operator_str=">", value=-1e9)
        with pytest.raises(RuntimeError, match="reader build failed"):
            leaf.evaluate()


def test_the_entry_pass_still_propagates_a_malformed_manifest(dispatcher, instances, clock,
                                                             monkeypatch):
    """The entry pass is NOT guarded: refusing entries is the safe reading of a broken manifest."""
    from ba2_trade_platform.core.TradeManager import TradeManager

    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    instances[3] = "ohlcv-v1"
    dispatcher._environ = {live.MANIFEST_ENV: "no-such-profile=abc"}
    ran = []
    monkeypatch.setattr(TradeManager, "_process_expert_recommendations_after_analysis",
                        lambda self, expert_id, lookback_days=1: ran.append(expert_id) or [])
    with pytest.raises(ValueError, match="no-such-profile"):
        TradeManager().process_expert_recommendations_after_analysis(3)
    assert ran == []


def test_real_evaluator_other_exit_rules_still_fire_after_a_failed_scope(dispatcher, instances,
                                                                         clock, monkeypatch):
    """End to end through the REAL ``TradeActionEvaluator`` with no decision scope open and a
    malformed manifest: rule 1 (a market leaf) reads no_context and does not fire, rule 2 (a
    plain account condition) still fires. Before the fix the leaf raised and the whole ruleset
    evaluation for the symbol raised with it."""
    from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import EventAction, Ruleset
    from ba2_trade_platform.core.types import ExpertActionType, ExpertEventRuleType
    from tests.conftest import MockAccount
    from tests.factories import link_rule_to_ruleset

    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    account_def = create_account_definition()
    expert_instance = create_expert_instance(account_id=account_def.id)
    rec = create_recommendation(instance_id=expert_instance.id, symbol="AAPL",
                                recommended_action=OrderRecommendation.BUY)
    instances[expert_instance.id] = "ohlcv-v1"
    dispatcher._environ = {live.MANIFEST_ENV: "no-such-profile=abc"}

    rs_id = add_instance(Ruleset(name="exits",
                                 type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE))
    rules = [
        ("market leaf", {"event_type": ExpertEventType.N_UNDERLYING_ADX.value,
                         "operator": ">", "value": -1e9}),
        ("plain rule", {"event_type": ExpertEventType.F_HAS_NO_POSITION_ACCOUNT.value}),
    ]
    for i, (name, trigger) in enumerate(rules):
        ea_id = add_instance(EventAction(
            name=name, type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            triggers={"trigger_0": trigger},
            actions={"action_0": {"action_type": ExpertActionType.BUY.value}},
            continue_processing=True))
        link_rule_to_ruleset(rs_id, ea_id, order_index=i)
    account = MockAccount(account_def.id)
    account._positions = []                       # confirmed flat: the plain rule is TRUE

    evaluator = TradeActionEvaluator(account=account)
    results = evaluator.evaluate("AAPL", rec, rs_id)

    assert results and not any("error" in r for r in results), results
    by_rule = {r["rule_name"]: r for r in evaluator.rule_evaluations}
    assert by_rule["plain rule"]["executed"] is True
    assert by_rule["market leaf"]["executed"] is False
    assert "error" not in by_rule["market leaf"]
    assert clock == []


def test_a_never_absorbed_refusal_still_propagates(world, activities, monkeypatch):
    """The guard uses the sanctioned ``absorb_if_benign(e, Exception)``, so the refusals
    ``failure_modes`` never absorbs in any mode (matched by class name) keep their meaning."""
    class SplitBasisRefused(Exception):
        pass

    monkeypatch.setattr(live, "market_condition_decision_scope",
                        _raising_scope(SplitBasisRefused("stop")))
    with pytest.raises(SplitBasisRefused):
        _run_with(world["expert_id"], _LogSpy())
    assert world["recorder"].calls == [] and activities == []


class _BodyBoom(Exception):
    pass


@pytest.mark.parametrize("scope_opens", [True, False], ids=["scope-open", "scope-failed"])
def test_an_exception_in_the_pass_body_still_propagates(world, activities, monkeypatch,
                                                        scope_opens):
    """The guard covers the scope's OPENING only. An exception raised by the pass body -- here
    from the log line between loading the recommendations and evaluating them, outside the
    per-recommendation handler -- propagates as it did before, the scope is closed on the way
    out, and the processing lock is released."""
    from ba2_trade_platform.core.TradeManager import TradeManager

    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    opened = []
    if scope_opens:
        monkeypatch.setattr(live, "market_condition_decision_scope",
                            _recording_scope(world, opened))
    else:
        monkeypatch.setattr(live, "market_condition_decision_scope",
                            _raising_scope(RuntimeError("scope")))
    tm = TradeManager()
    tm.logger = _LogSpy(raise_on_info=_BodyBoom("body"))

    with pytest.raises(_BodyBoom):
        tm.process_open_positions_recommendations(world["expert_id"])

    assert world["recorder"].calls == []
    assert world["state"]["scope_depth"] == 0
    assert opened == ([world["expert_id"]] if scope_opens else [])
    assert len(activities) == (0 if scope_opens else 1)
    lock = tm._processing_locks[f"expert_{world['expert_id']}_usecase_open_positions"]
    assert not lock.locked()


# =========================================================================== hardening (I1/I2/M1/M2)
@pytest.fixture(autouse=True)
def _fresh_exit_scope_failure_reports(monkeypatch):
    """The guard's once-per-cause ERROR memory is process-wide by design; each test starts clean
    (expert ids restart at 1 with every fresh test DB, so keys would collide across tests)."""
    from ba2_trade_platform.core.TradeManager import TradeManager

    monkeypatch.setattr(TradeManager, "_exit_scope_failures_reported", set())


class _PkgLogSpy:
    """Stands in for ``ba2_common.logger.logger`` (propagate=False: caplog never sees it)."""

    def __init__(self):
        self.records = []

    def error(self, msg, *a, **k):
        self.records.append(("ERROR", str(msg)))

    def warning(self, msg, *a, **k):
        self.records.append(("WARNING", str(msg)))

    def info(self, msg, *a, **k):
        self.records.append(("INFO", str(msg)))

    def debug(self, msg, *a, **k):
        self.records.append(("DEBUG", str(msg)))

    def levels(self, needle):
        return [lvl for lvl, msg in self.records if needle in msg]


def _counting_failing_build(dispatcher, monkeypatch, exc):
    calls = []

    def _build(profiles):
        calls.append(profiles)
        raise exc

    monkeypatch.setattr(dispatcher, "_build", _build)
    return calls


def test_after_a_failed_scope_leaves_answer_with_the_guards_cause_and_never_rebuild(
        world, dispatcher, instances, clock, activities, monkeypatch):
    """I2: the guard's failed attempt is the ONLY resolver build of the pass. Every leaf of that
    expert then reads no_context with the guard's cause, without retrying the lookup."""
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    instances[world["expert_id"]] = "ohlcv-v1"
    builds = _counting_failing_build(dispatcher, monkeypatch, RuntimeError("reader build failed"))

    def _two_leaves(account, symbol, rec):
        return [_adx_leaf(account, symbol, rec), _adx_leaf(account, symbol, rec)]

    world["recorder"].leaf = _two_leaves
    result = _run_with(world["expert_id"], _LogSpy())

    assert len(builds) == 1                                  # the guard's attempt, nothing more
    (call,) = world["recorder"].calls
    expected = live.no_scope_open_reason(world["expert_id"], "RuntimeError: reader build failed",
                                         live.UNRESOLVED_SCOPE_FAILED)
    for got in call["leaf"]:
        assert got["status"] == STATUS_NO_CONTEXT and got["passed"] is False
        assert got["reason"] == expected
    assert "could not open its market-condition decision scope" in expected
    assert result == [{"success": True, "symbol": SYMBOL, "submit_to_broker": True}]
    # The pass is over: its exit-pass mark is gone.
    assert live.current_exit_pass() is None


def test_a_resolver_rebuild_failure_inside_the_exit_scope_reads_no_context(
        world, dispatcher, instances, clock, monkeypatch):
    """M1, exit side: the scope opened, then (e.g. after a mid-pass /api/reload) the expert's
    resolver cannot be rebuilt. The leaf reads no_context with the cause; the pass carries on."""
    import ba2_common.logger as bl

    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    pkg_log = _PkgLogSpy()
    monkeypatch.setattr(bl, "logger", pkg_log)
    instances[world["expert_id"]] = "ohlcv-v1"

    def _reload_then_leaf(account, symbol, rec):
        assert live.current_decision() is not None and live.current_exit_pass() is not None
        dispatcher.clear_cache()
        _counting_failing_build(dispatcher, monkeypatch, RuntimeError("rebuild failed"))
        return _adx_leaf(account, symbol, rec)

    world["recorder"].leaf = _reload_then_leaf
    result = _run(world["expert_id"])

    (call,) = world["recorder"].calls
    got = call["leaf"]
    assert got["status"] == STATUS_NO_CONTEXT and got["passed"] is False
    assert got["reason"] == live.no_scope_open_reason(
        world["expert_id"], "RuntimeError: rebuild failed", live.UNRESOLVED_EXIT_SCOPE)
    # _adx_leaf dispatches twice (the explicit resolve + the leaf): WARNING once, then DEBUG.
    levels = pkg_log.levels("rebuild failed")
    assert levels[0] == "WARNING" and set(levels[1:]) <= {"DEBUG"} and len(levels) == 2
    assert result == [{"success": True, "symbol": SYMBOL, "submit_to_broker": True}]


def test_a_resolver_rebuild_failure_inside_the_entry_scope_still_raises(
        dispatcher, instances, clock, monkeypatch):
    """M1, entry side: the same mid-pass rebuild failure inside the ENTER-MARKET pass propagates
    out of the leaf, exactly as before."""
    from ba2_trade_platform.core.TradeManager import TradeManager

    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    instances[4] = "ohlcv-v1"
    raised = []

    def _inner(self, expert_id, lookback_days=1):
        assert live.current_decision() is not None and live.current_exit_pass() is None
        dispatcher.clear_cache()
        _counting_failing_build(dispatcher, monkeypatch, RuntimeError("rebuild failed"))
        leaf = TC.create_condition(ExpertEventType.N_UNDERLYING_ADX, object(), SYMBOL,
                                   _rec_for(4), operator_str=">", value=-1e9)
        try:
            leaf.evaluate()
        except RuntimeError as e:
            raised.append(e)
            raise
        return []

    monkeypatch.setattr(TradeManager, "_process_expert_recommendations_after_analysis", _inner)
    with pytest.raises(RuntimeError, match="rebuild failed"):
        TradeManager().process_expert_recommendations_after_analysis(4)
    assert len(raised) == 1


def test_the_guard_logs_error_once_then_warning_but_records_every_pass(world, activities,
                                                                       monkeypatch):
    """M2: a persistent fault logs its traceback once per (expert, exception type) per process,
    then WARNING without a traceback; the FAILURE activity row is written EVERY pass."""
    monkeypatch.setattr(live, "market_condition_decision_scope",
                        _raising_scope(ValueError("bad manifest")))
    first, second = _LogSpy(), _LogSpy()
    _run_with(world["expert_id"], first)
    _run_with(world["expert_id"], second)

    def scope_lines(spy):
        return [(lvl, exc) for lvl, msg, exc in spy.records
                if "decision scope could not be opened" in msg]

    assert scope_lines(first) == [("ERROR", True)]
    assert scope_lines(second) == [("WARNING", False)]
    assert len(activities) == 2
    # A DIFFERENT exception type for the same expert is a new cause: ERROR again.
    monkeypatch.setattr(live, "market_condition_decision_scope",
                        _raising_scope(TypeError("seam defect")))
    third = _LogSpy()
    _run_with(world["expert_id"], third)
    assert scope_lines(third) == [("ERROR", True)]


def test_the_dispatcher_warns_once_per_cause_then_debug(dispatcher, instances, clock,
                                                       monkeypatch):
    """I1: an absorbed dispatch failure warns once per (expert, exception type), then DEBUG."""
    import ba2_common.logger as bl

    pkg_log = _PkgLogSpy()
    monkeypatch.setattr(bl, "logger", pkg_log)
    instances[5] = "ohlcv-v1"
    _counting_failing_build(dispatcher, monkeypatch, RuntimeError("reader build failed"))
    for _ in range(3):
        leaf = TC.create_condition(ExpertEventType.N_UNDERLYING_ADX, object(), SYMBOL,
                                   _rec_for(5), operator_str=">", value=-1e9)
        assert leaf.evaluate() is False and leaf.last_status == STATUS_NO_CONTEXT
    assert pkg_log.levels("reader build failed") == ["WARNING", "DEBUG", "DEBUG"]


def test_exit_pass_state_and_last_dispatch_do_not_cross_threads(dispatcher, instances, clock):
    """Two concurrent passes on two threads: each sees only its own exit-pass mark and its own
    last dispatch, so neither reads the other's reason."""
    import threading

    instances[1] = "ohlcv-v1"
    instances[8] = ""
    barrier = threading.Barrier(2, timeout=10)
    seen = {}
    errors = []

    def exit_thread():
        try:
            end = live.begin_exit_pass(1)
            try:
                live.record_exit_pass_scope_failure(ValueError("bad manifest"))
                seen["a_leaf"] = _adx_leaf(object(), SYMBOL, _rec_for(1))
                barrier.wait()          # B dispatches now, on its own thread
                barrier.wait()
                seen["a_reason_after"] = dispatcher.no_context_reason_for(SYMBOL)
                seen["a_exit"] = live.current_exit_pass()
            finally:
                end()
        except Exception as e:  # noqa: BLE001 -- surfaced by the assertion below
            errors.append(e)

    def plain_thread():
        try:
            barrier.wait()
            seen["b_exit"] = live.current_exit_pass()
            seen["b_last_before"] = live._LAST_DISPATCH.get()
            seen["b_leaf"] = _adx_leaf(object(), SYMBOL, _rec_for(8))
            barrier.wait()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=exit_thread), threading.Thread(target=plain_thread)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []

    scope_failed = live.no_scope_open_reason(1, "ValueError: bad manifest",
                                             live.UNRESOLVED_SCOPE_FAILED)
    assert seen["a_leaf"]["reason"] == scope_failed
    assert seen["a_reason_after"] == scope_failed          # B's dispatch did not overwrite A's
    assert seen["a_exit"].expert_instance_id == 1
    assert seen["b_exit"] is None and seen["b_last_before"] is None
    assert "expert instance 8 has an empty market_condition_profile" in seen["b_leaf"]["reason"]
    # And nothing leaked into the test's own thread.
    assert live.current_exit_pass() is None


def test_run_in_decision_context_carries_the_exit_pass_to_a_pool_thread(dispatcher, instances,
                                                                        clock):
    """A pool thread of an exit pass absorbs like the coordinating thread (the wrapper carries
    the exit-pass mark with the decision state)."""
    from concurrent.futures import ThreadPoolExecutor

    instances[1] = "ohlcv-v1"
    end = live.begin_exit_pass(1)
    try:
        live.record_exit_pass_scope_failure(ValueError("bad manifest"))
        wrapped = live.run_in_decision_context(
            lambda: (live.current_exit_pass(), _adx_leaf(object(), SYMBOL, _rec_for(1))))
    finally:
        end()
    with ThreadPoolExecutor(max_workers=1) as pool:
        exit_state, got = pool.submit(wrapped).result(timeout=20)
        assert pool.submit(live.current_exit_pass).result(timeout=20) is None
    assert exit_state is not None and exit_state.expert_instance_id == 1
    assert got["reason"] == live.no_scope_open_reason(1, "ValueError: bad manifest",
                                                      live.UNRESOLVED_SCOPE_FAILED)
