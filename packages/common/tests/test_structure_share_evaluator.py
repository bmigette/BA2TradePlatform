"""The evaluator hands a structure_share condition its rule's entry tag; for every other
condition it is a no-op (plan 2026-11-06, Task 8)."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import ba2_common.core.OptionRiskManagement as rm
import ba2_common.core.entry_tag as entry_tag_mod
from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_common.core.types import ExpertEventType

EID, CSP = 7, 11
AT = datetime(2026, 11, 2, 10, 0)


def _rec():
    return SimpleNamespace(instance_id=EID, created_at=AT)


@pytest.fixture
def share_rig(monkeypatch):
    rm.reset_state()
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    monkeypatch.setattr(rm, "transaction_entry_tag", lambda tid: None)
    monkeypatch.setattr(rm, "assigned_stock_lots", lambda eid: [])
    monkeypatch.setattr(rm, "_live_transactions", lambda eid: None)
    expert = SimpleNamespace(settings={"risk_manager_mode": "classic_options"})
    monkeypatch.setattr("ba2_common.core.instance_resolver.get_instance_resolver",
                        lambda: SimpleNamespace(get_expert_instance=lambda i: expert))
    account = SimpleNamespace(get_account_snapshot=lambda: SimpleNamespace(equity=100_000.0))
    yield account
    rm.reset_state()


def _action(trigger, actions):
    return SimpleNamespace(name="r", id=1, triggers={"t0": trigger}, actions=actions)


def test_a_structure_share_leaf_is_bound_to_its_rules_entry_tag_and_evaluates(share_rig):
    ev = TradeActionEvaluator(share_rig, "AAPL")
    action = _action({"event_type": "structure_share", "operator": "<", "value": 30.0},
                     {"a0": {"action_type": "buy_call", "entry_tag": CSP}})
    assert ev._evaluate_conditions(action, "AAPL", _rec(), None) is True
    cond = ev.rule_evaluations[0]["conditions"][0]
    assert cond["event_type"] == "structure_share" and cond["calculated_value"] == 0.0


def test_a_rule_whose_entry_action_has_no_tag_fails_the_leaf(share_rig):
    ev = TradeActionEvaluator(share_rig, "AAPL")
    action = _action({"event_type": "structure_share", "operator": "<", "value": 30.0},
                     {"a0": {"action_type": "buy_call"}})
    assert ev._evaluate_conditions(action, "AAPL", _rec(), None) is False


def test_the_binding_is_a_no_op_when_the_rule_has_no_structure_share_leaf(monkeypatch):
    """GA neutrality (c): no structure_share leaf -> the tag helper is never called."""
    def boom(*a, **k):
        raise AssertionError("entry_tag_of_actions must not run without a structure_share leaf")

    monkeypatch.setattr(entry_tag_mod, "entry_tag_of_actions", boom)
    account = MagicMock()
    account.get_iv_rank.return_value = 25.0
    ev = TradeActionEvaluator(account, "AAPL")
    action = _action({"event_type": ExpertEventType.N_IV_RANK.value, "operator": "<=",
                      "value": 40.0},
                     {"a0": {"action_type": "buy_call", "entry_tag": CSP}})
    before = ev.rule_evaluations[:]
    ev._evaluate_conditions(action, "AAPL", None, None)
    assert [c["event_type"] for c in ev.rule_evaluations[0]["conditions"]] == ["iv_rank"]
    assert before == []


def test_only_structure_share_defines_the_hook():
    from ba2_common.core.TradeConditions import CONDITION_MAP
    owners = {et.value for et, cls in CONDITION_MAP.items() if hasattr(cls, "bind_event_action")}
    assert owners == {"structure_share"}
