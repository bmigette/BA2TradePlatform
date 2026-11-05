"""IVRankCondition shows its actual rank, or WHY the rank is unavailable, in the
Rule Evaluation Details dialog, and the text is stored in the persisted evaluation details."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_common.core.TradeConditions import create_condition
from ba2_common.core.types import ExpertEventType


def _account(rank, reason="__default__"):
    acct = MagicMock(spec=OptionsAccountInterface)
    acct.get_iv_rank.return_value = rank
    if reason != "__default__":
        acct.iv_rank_unavailable_reason.return_value = reason
    return acct


def _cond(acct):
    return create_condition(ExpertEventType.N_IV_RANK, acct, "AAPL", None,
                            operator_str="<=", value=40.0)


def test_interface_default_reason_is_none():
    assert OptionsAccountInterface.iv_rank_unavailable_reason(object(), "AAPL") is None


def test_rank_present_shows_actual():
    c = _cond(_account(33.33))
    assert c.evaluate() is True
    assert c.get_actual_value_display() == "33.3"
    assert c.unavailable_reason is None


def test_rank_none_with_reason_shows_unavailable():
    why = "no derived ATM IV for the last completed session 2026-10-09"
    c = _cond(_account(None, why))
    assert c.evaluate() is False
    assert c.get_actual_value_display() == f"unavailable: {why}"


def test_rank_none_without_reason_unchanged():
    c = _cond(_account(None, None))
    assert c.evaluate() is False
    assert c.get_actual_value_display() is None


def test_reason_lookup_failure_never_breaks_evaluation():
    acct = _account(None)
    acct.iv_rank_unavailable_reason.side_effect = RuntimeError("boom")
    c = _cond(acct)
    assert c.evaluate() is False
    assert c.get_actual_value_display() is None


def test_non_options_account_reason():
    c = _cond(MagicMock())  # not an OptionsAccountInterface
    assert c.evaluate() is False
    assert c.get_actual_value_display() == "unavailable: account does not support options"


def test_stale_reason_cleared_on_next_evaluate():
    acct = _account(None, "history filling 120/252")
    c = _cond(acct)
    c.evaluate()
    acct.get_iv_rank.return_value = 10.0
    assert c.evaluate() is True
    assert c.get_actual_value_display() == "10.0"


def _persisted(acct):
    ev = TradeActionEvaluator(acct, "AAPL")
    action = SimpleNamespace(name="r", id=1, triggers={"t0": {
        "event_type": ExpertEventType.N_IV_RANK.value, "operator": "<=", "value": 40.0}})
    ev._evaluate_conditions(action, "AAPL", None, None)
    details = json.loads(json.dumps(ev.get_evaluation_details(), default=str))
    return details["rule_evaluations"][0]["conditions"][0]


def test_persisted_details_round_trip():
    cond = _persisted(_account(25.0))
    assert cond["actual_value_display"] == "25.0"
    why = "history filling 120/252"
    cond = _persisted(_account(None, why))
    assert cond["actual_value_display"] == f"unavailable: {why}"
    assert cond["condition_result"] is False
    cond = _persisted(_account(None, None))
    assert "actual_value_display" not in cond
