from types import SimpleNamespace as NS

from ba2_common.core.rules_export_import import ruleset_export_body, rulesets_export_envelope
from ba2_common.core.types import AnalysisUseCase, ExpertEventRuleType


def _rule(name):
    return NS(name=name, type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE, subtype=None,
              triggers={"t0": {"event_type": "bullish"}}, actions={"a0": {"action_type": "buy"}},
              extra_parameters={}, continue_processing=False)


def test_body_shape_order_and_generated_names():
    rs = NS(name="RS", description="d", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            subtype=AnalysisUseCase.ENTER_MARKET)
    body = ruleset_export_body(rs, [(0, _rule("My Rule")), (1, _rule("rule 1"))])
    assert list(body) == ["name", "description", "type", "subtype", "rules"]
    assert body["type"] == "trading_recommendation_rule"
    assert body["subtype"] == "enter_market"
    assert body["rules"][0]["name"] == "My Rule"
    assert body["rules"][1]["name"] != "rule 1"          # generic -> generated
    assert body["rules"][1]["order_index"] == 1
    assert list(body["rules"][0]) == ["name", "type", "subtype", "triggers", "actions",
                                      "extra_parameters", "continue_processing", "order_index"]


def test_envelope():
    env = rulesets_export_envelope([{"name": "A"}], exported_at="2026-01-01T00:00:00")
    assert env == {"export_version": "1.0", "export_type": "rulesets",
                   "export_timestamp": "2026-01-01T00:00:00", "rulesets": [{"name": "A"}]}
