# packages/common/tests/test_rule_builders_wing.py
from ba2_common.core.rule_builders import action_from_rule


def test_action_from_rule_forwards_wing_width():
    rule = {"action_type": "open_iron_condor", "option_strike_param": 10.0,
            "option_dte_min": 20, "option_dte_max": 40, "option_sizing": 20.0,
            "option_wing_width_pct": 5.0}
    out = action_from_rule(rule)
    cfg = out["act"]
    assert cfg["action_type"] == "open_iron_condor"
    assert cfg["wing_width_pct"] == 5.0
    assert cfg["strike_param"] == 10.0
