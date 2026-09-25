"""A trigger the evaluator cannot read FAILS its rule; it never lets the rule pass.

``TradeActionEvaluator._evaluate_conditions`` used to ``continue`` past a trigger with no
``event_type``, or with one that is not an ``ExpertEventType`` (a rollback to an older
ba2_common, or a rule imported from newer code), WITHOUT marking the rule unmet. The rule's
other triggers then decided alone, and a single-trigger rule fired unconditionally. The
"Could not create condition" branch right after it already failed the rule; these pin the two
parse branches to the same behaviour, in both evaluation modes.

A rule with NO triggers at all is unchanged: it is always true.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_common.core.types import ExpertEventType, OrderRecommendation

SYMBOL = "AAPL"

VALID = {"event_type": ExpertEventType.F_BULLISH.value}          # true for a BUY recommendation
VALID_2 = {"event_type": ExpertEventType.F_BEARISH.value}        # false for a BUY recommendation
MISSING = {"operator": ">", "value": 1}                            # no event_type at all
EMPTY = {"event_type": "", "operator": ">", "value": 1}            # falsy event_type
UNKNOWN = {"event_type": "from_newer_code_not_an_event_type", "operator": ">", "value": 1}


class _Account:
    id = 1


def _rec():
    return SimpleNamespace(created_at=datetime(2026, 9, 24, tzinfo=timezone.utc), instance_id=1,
                           symbol=SYMBOL, data={}, confidence=80.0,
                           recommended_action=OrderRecommendation.BUY)


def _evaluate(triggers, evaluate_all_conditions):
    ev = TradeActionEvaluator(account=_Account(), evaluate_all_conditions=evaluate_all_conditions)
    action = SimpleNamespace(name="r", id=1, triggers=triggers)
    return ev, ev._evaluate_conditions(action, SYMBOL, _rec(), None)


MODES = pytest.mark.parametrize("evaluate_all", [False, True], ids=["first_failure", "evaluate_all"])


@MODES
@pytest.mark.parametrize("bad", [MISSING, EMPTY], ids=["missing", "empty"])
def test_single_trigger_with_missing_event_type_does_not_fire(bad, evaluate_all):
    ev, met = _evaluate({"cond_0": bad}, evaluate_all)
    assert met is False
    rule = ev.rule_evaluations[-1]
    assert rule["all_conditions_met"] is False
    assert rule["executed"] is False
    assert rule["conditions"][0]["error"] == "No event_type specified"


@MODES
def test_single_trigger_with_invalid_event_type_does_not_fire(evaluate_all):
    ev, met = _evaluate({"cond_0": UNKNOWN}, evaluate_all)
    assert met is False
    rule = ev.rule_evaluations[-1]
    assert rule["all_conditions_met"] is False
    assert rule["executed"] is False
    assert rule["conditions"][0]["error"].startswith("Invalid event type")


@MODES
@pytest.mark.parametrize("order", ["valid_first", "invalid_first"])
@pytest.mark.parametrize("bad", [MISSING, UNKNOWN], ids=["missing", "unknown"])
def test_valid_trigger_plus_unreadable_one_does_not_fire(bad, order, evaluate_all):
    triggers = ({"cond_0": VALID, "cond_1": bad} if order == "valid_first"
                else {"cond_0": bad, "cond_1": VALID})
    ev, met = _evaluate(triggers, evaluate_all)
    assert met is False
    rule = ev.rule_evaluations[-1]
    assert rule["all_conditions_met"] is False
    assert rule["executed"] is False
    # both triggers are still recorded: the unreadable one does not hide the readable one
    assert len(rule["conditions"]) == 2


@MODES
def test_only_valid_triggers_are_unchanged(evaluate_all):
    ev, met = _evaluate({"cond_0": VALID}, evaluate_all)
    assert met is True
    assert ev.rule_evaluations[-1]["executed"] is True

    ev, met = _evaluate({"cond_0": VALID, "cond_1": VALID_2}, evaluate_all)
    assert met is False
    assert ev.rule_evaluations[-1]["all_conditions_met"] is False


@MODES
@pytest.mark.parametrize("triggers", [{}, None], ids=["empty_dict", "none"])
def test_rule_with_no_triggers_is_always_true(triggers, evaluate_all):
    ev, met = _evaluate(triggers, evaluate_all)
    assert met is True
    rule = ev.rule_evaluations[-1]
    assert rule["all_conditions_met"] is True
    assert rule["executed"] is True
