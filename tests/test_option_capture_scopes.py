"""The decision-pass capture scope opens for OPTION experts on both passes (option capture replay, Task 4).

The scope used to open only for an expert with a market-condition resolver. The option taps and the
per-action option record run inside the rule evaluation of the enter-market and open-positions passes,
so an expert on an ``OptionsAccountInterface`` account needs a scope too. A stock expert without a
profile must keep getting NO scope: it would otherwise add a synthetic "TradeManager" analysis to
every capture session and move the replay coverage of deployments that use neither feature.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext

import pytest

import ba2_common.core.market_condition_live as mcl
import ba2_common.core.replay as replay_pkg
import ba2_trade_platform.core.utils as core_utils
from ba2_common.core import market_condition_live as live
from ba2_trade_platform.core.interfaces import OptionsAccountInterface
from ba2_trade_platform.core.types import AnalysisUseCase
from tests.factories import create_account_definition, create_expert_instance
from tests.test_open_positions_market_condition_scope import (  # noqa: F401 -- fixtures
    SYMBOL,
    _run,
    world,
)


class _Store:
    def __init__(self, session_id="s1"):
        self._sid = session_id

    def current_session_id(self):
        return self._sid


@pytest.fixture
def opened(monkeypatch):
    """Records every capture_scope(store, meta) call; installs a store."""
    calls = []

    @contextmanager
    def _scope(store, meta):
        calls.append(meta)
        yield

    monkeypatch.setattr(replay_pkg, "capture_scope", _scope)
    monkeypatch.setattr(replay_pkg, "get_replay_store", lambda: _Store())
    monkeypatch.setattr(mcl, "resolver_for_expert_instance", lambda expert_id: None)
    return calls


def _expert_with(option_rules, *, exit_rule=False):
    """An expert whose ruleset holds an option action (or only a stock action).

    ``exit_rule`` puts the option action in the open-positions ruleset instead of the entry one.
    """
    from ba2_trade_platform.core.types import ExpertActionType
    from tests.factories import create_event_action, create_ruleset, link_rule_to_ruleset

    acct_def = create_account_definition()
    action = (ExpertActionType.CLOSE_OPTION if exit_rule else ExpertActionType.BUY_CALL)         if option_rules else ExpertActionType.BUY
    rule = create_event_action(actions={"a1": {"action_type": action.value}})
    ruleset = create_ruleset()
    link_rule_to_ruleset(ruleset.id, rule.id)
    kw = {"open_positions_ruleset_id": ruleset.id} if exit_rule else {"enter_market_ruleset_id": ruleset.id}
    expert = create_expert_instance(account_id=acct_def.id, **kw)
    return expert.id


def _scope(expert_id, use_case=None):
    from ba2_trade_platform.core.TradeManager import TradeManager

    cm = (TradeManager()._decision_capture_scope(expert_id) if use_case is None
          else TradeManager()._decision_capture_scope(expert_id, use_case))
    with cm:
        pass


def test_option_ruleset_expert_without_profile_opens_the_entry_scope(opened):
    expert_id = _expert_with(True)
    _scope(expert_id)
    assert len(opened) == 1
    meta = opened[0]
    assert meta["expert_class"] == "TradeManager"
    assert meta["use_case"] == AnalysisUseCase.ENTER_MARKET.value
    assert meta["analysis_id"].startswith(f"decision-{expert_id}-")
    assert meta["expert_instance_id"] == expert_id and meta["symbol"] == ""


def test_option_exit_rule_opens_an_exit_scope_with_a_distinguishable_id(opened):
    expert_id = _expert_with(True, exit_rule=True)
    _scope(expert_id, AnalysisUseCase.OPEN_POSITIONS)
    assert opened[0]["use_case"] == AnalysisUseCase.OPEN_POSITIONS.value
    assert opened[0]["analysis_id"].startswith(f"exit-decision-{expert_id}-")


def test_stock_ruleset_expert_stays_unrecorded_on_both_passes(opened):
    expert_id = _expert_with(False)
    _scope(expert_id)
    _scope(expert_id, AnalysisUseCase.OPEN_POSITIONS)
    assert opened == []


def test_expert_without_rulesets_stays_unrecorded(opened):
    acct_def = create_account_definition()
    expert = create_expert_instance(account_id=acct_def.id)
    _scope(expert.id)
    assert opened == []


def test_stock_expert_on_the_real_alpaca_account_class_gets_no_scope(opened):
    """AlpacaAccount IS an OptionsAccountInterface for every expert on it, stock ones included:
    the class must not be the gate."""
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    assert issubclass(AlpacaAccount, OptionsAccountInterface)
    acct_def = create_account_definition(provider="AlpacaAccount")
    from tests.factories import create_event_action, create_ruleset, link_rule_to_ruleset
    rule = create_event_action(actions={"a1": {"type": "buy"}})
    rs = create_ruleset()
    link_rule_to_ruleset(rs.id, rule.id)
    expert = create_expert_instance(account_id=acct_def.id, enter_market_ruleset_id=rs.id)
    _scope(expert.id)
    assert opened == []


def test_gated_expert_still_opens_without_reading_the_rulesets(opened, monkeypatch):
    from ba2_trade_platform.core.TradeManager import TradeManager

    expert_id = _expert_with(False)
    monkeypatch.setattr(mcl, "resolver_for_expert_instance", lambda eid: object())
    calls = []
    monkeypatch.setattr(TradeManager, "_expert_has_option_rules",
                        staticmethod(lambda eid: calls.append(eid) or False))
    _scope(expert_id)
    assert len(opened) == 1 and calls == []


def test_capture_off_is_a_null_context_and_never_reads_the_rulesets(opened, monkeypatch):
    from ba2_trade_platform.core.TradeManager import TradeManager

    expert_id = _expert_with(True)
    monkeypatch.setattr(replay_pkg, "get_replay_store", lambda: None)
    calls = []
    monkeypatch.setattr(TradeManager, "_expert_has_option_rules",
                        staticmethod(lambda eid: calls.append(eid) or True))
    _scope(expert_id)
    assert opened == [] and calls == []


def test_ruleset_resolution_failure_is_an_error_and_a_null_context(opened, monkeypatch):
    from ba2_trade_platform.core import TradeManager as tm_mod
    from ba2_trade_platform.core.TradeManager import TradeManager

    expert_id = _expert_with(True)

    def _boom(eid):
        raise RuntimeError("db gone")

    monkeypatch.setattr(TradeManager, "_expert_has_option_rules", staticmethod(_boom))
    errors = []
    monkeypatch.setattr(tm_mod.logger, "error", lambda msg, *a, **k: errors.append(msg))
    _scope(expert_id)
    assert opened == []
    assert len(errors) == 1 and str(expert_id) in errors[0]


def test_open_positions_pass_opens_the_capture_scope_outside_the_market_condition_scope(
        world, opened, monkeypatch):
    """Order is load-bearing: the capture scope is entered BEFORE the market-condition scope."""
    order = []

    @contextmanager
    def _cap(store, meta):
        order.append(("capture", meta["use_case"]))
        yield
        order.append(("capture-closed", None))

    @contextmanager
    def _mc(*, expert_instance_id=None, replay_reader=None):
        order.append(("market", None))
        yield None

    monkeypatch.setattr(replay_pkg, "capture_scope", _cap)
    monkeypatch.setattr(live, "market_condition_decision_scope", _mc)
    # the world's expert sits on an account whose class is the fake _FakeAccount (not options):
    # make the account check say options
    from ba2_trade_platform.core.TradeManager import TradeManager
    monkeypatch.setattr(TradeManager, "_expert_has_option_rules",
                        staticmethod(lambda expert_id: True))

    _run(world["expert_id"])

    kinds = [k for k, _ in order]
    assert kinds.index("capture") < kinds.index("market") < kinds.index("capture-closed")
    assert dict(order)["capture"] == AnalysisUseCase.OPEN_POSITIONS.value
