"""Every close rule the grid launcher can EMIT is classified to its intended close reason.

``TradeActionEvaluator.option_close_trigger`` records WHY an option position closed (BT/live
option parity, plan Part C3). The rule cases here are GENERATED from the launcher's real rule
builders -- ``_option_exit_rules`` / ``_overlay_rules`` / ``_covered_call_exit_rule`` over every
option key the launcher knows -- rather than hand-copied, so a new close rule fails this test
until someone states what it is.
"""
import importlib.util
import os
import sys

import pytest

from ba2_common.core.TradeActionEvaluator import option_close_trigger
from ba2_common.core.rule_builders import triggers_from_condition_tree
from ba2_common.core.types import OptionCloseReason

_LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "ba2test_launcher.py")

#: THE TABLE. A close rule the launcher emits that is missing here fails the test.
EXPECTED = {
    "opt_tp": OptionCloseReason.TAKE_PROFIT,
    "opt_tp_mult": OptionCloseReason.TAKE_PROFIT,
    "opt_sl": OptionCloseReason.STOP_LOSS,
    "opt_sl_ml": OptionCloseReason.STOP_LOSS,
    "opt_time": OptionCloseReason.TIME_EXIT,
    "opt_event": OptionCloseReason.TIME_EXIT,
    "opt_dte": OptionCloseReason.DTE_EXIT,
    "cc_dte": OptionCloseReason.DTE_EXIT,
    # A PROTECTIVE exit on the long leg's delta: not a P&L stop and not a schedule, so it is
    # recorded as a rule exit (see OptionCloseReason.RULE_EXIT).
    "pmcc_delta_floor": OptionCloseReason.RULE_EXIT,
}


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_close_trigger", _LAUNCHER)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_close_trigger"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def _emitted_close_rules():
    m = _launcher()
    kinds = (set(m._OPTION_STRATS) | set(m._OPTION_GROUPS_ALL)
             | set(m._OVERLAY_ROLL_KINDS) | set(m._COVERED_CALL_OVERLAY_KINDS))
    seen = {}
    for kind in sorted(kinds):
        rules = (list(m._option_exit_rules(kind)) + list(m._overlay_rules(kind))
                 + list(m._covered_call_exit_rule(kind)))
        for rule in rules:
            if rule.get("action_type") != "close_option":
                continue
            seen.setdefault(rule["id"], []).append((kind, rule))
    return seen


_RULES = _emitted_close_rules()


def test_the_launcher_emits_the_close_rules_this_table_knows():
    unknown = sorted(set(_RULES) - set(EXPECTED))
    assert not unknown, (f"the launcher emits close rule(s) {unknown} with no expected close "
                         f"reason: add them to EXPECTED (and check option_close_trigger)")
    assert set(_RULES) == set(EXPECTED), sorted(set(EXPECTED) - set(_RULES))


@pytest.mark.parametrize("rule_id", sorted(_RULES))
def test_each_emitted_close_rule_records_its_reason(rule_id):
    for kind, rule in _RULES[rule_id]:
        event_action = type("EA", (), {"triggers": triggers_from_condition_tree(
            rule["conditions"])})()
        assert option_close_trigger(event_action) is EXPECTED[rule_id], (kind, rule_id)
