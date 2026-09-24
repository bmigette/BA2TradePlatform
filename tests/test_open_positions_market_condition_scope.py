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


@pytest.mark.xfail(strict=True, raises=ValueError, reason=(
    "OPEN GAP, awaiting a decision: the scope guard covers the scope's OPENING only. A market "
    "leaf evaluated afterwards resolves its context through PerInstanceMarketConditionResolver."
    "__call__, which calls resolver_for() again and re-raises the malformed-manifest ValueError. "
    "Under BA2_ERROR_MODE=enforce neither TradeActionEvaluator handler absorbs it, so a real "
    "exit ruleset containing a market leaf still fails to evaluate for that symbol."))
def test_a_market_leaf_after_a_failed_scope_reads_no_context(world, dispatcher, instances, clock,
                                                            monkeypatch):
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    instances[world["expert_id"]] = "ohlcv-v1"
    dispatcher._environ = {live.MANIFEST_ENV: "no-such-profile=abc"}
    rec = type("Rec", (), {"instance_id": world["expert_id"], "symbol": SYMBOL})()
    # What the pass does after the guard absorbed the scope failure: evaluate the leaf with no
    # decision state. The desired outcome is "unknown, never passes", not an exception.
    got = _adx_leaf(object(), SYMBOL, rec)
    assert got["status"] == STATUS_NO_CONTEXT and got["passed"] is False


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
