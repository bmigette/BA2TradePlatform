"""Tests for RulesExporter and RulesImporter."""
import pytest
from ba2_trade_platform.core.rules_export_import import RulesExporter, RulesImporter
from ba2_trade_platform.core.models import Ruleset, EventAction
from ba2_trade_platform.core.types import ExpertEventRuleType, ExpertEventType, ExpertActionType
from ba2_trade_platform.core.db import add_instance, get_instance
from tests.factories import link_rule_to_ruleset


def _create_ruleset_with_rules():
    """Helper: create a ruleset with 2 linked rules."""
    rs = Ruleset(
        name="Export Test Ruleset",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        description="For testing export",
    )
    rs_id = add_instance(rs)

    ea1 = EventAction(
        name="Export Rule 1",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
        actions={"action_0": {"action_type": ExpertActionType.BUY.value}},
        continue_processing=True,
    )
    ea1_id = add_instance(ea1)
    link_rule_to_ruleset(rs_id, ea1_id, order_index=0)

    ea2 = EventAction(
        name="Export Rule 2",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": ExpertEventType.F_BEARISH.value}},
        actions={"action_0": {"action_type": ExpertActionType.SELL.value}},
        continue_processing=False,
    )
    ea2_id = add_instance(ea2)
    link_rule_to_ruleset(rs_id, ea2_id, order_index=1)

    return rs_id, [ea1_id, ea2_id]


class TestRulesExporter:
    def test_export_ruleset(self):
        rs_id, _ = _create_ruleset_with_rules()
        data = RulesExporter.export_ruleset(rs_id)
        assert data["export_version"] == "1.0"
        assert data["export_type"] == "ruleset"
        assert data["ruleset"]["name"] == "Export Test Ruleset"
        assert len(data["ruleset"]["rules"]) == 2

    def test_export_rule(self):
        ea = EventAction(
            name="Single Export Rule",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            triggers={"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
            actions={"action_0": {"action_type": ExpertActionType.BUY.value}},
        )
        ea_id = add_instance(ea)
        data = RulesExporter.export_rule(ea_id)
        assert data["export_type"] == "rule"
        assert data["rule"]["name"] == "Single Export Rule"

    def test_export_nonexistent_ruleset_raises(self):
        with pytest.raises(Exception):
            RulesExporter.export_ruleset(99999)

    def test_export_multiple_rulesets(self):
        rs_id1, _ = _create_ruleset_with_rules()
        # Create a second ruleset
        rs2 = Ruleset(
            name="Export Multi Test 2",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        )
        rs_id2 = add_instance(rs2)
        data = RulesExporter.export_multiple_rulesets([rs_id1, rs_id2])
        assert data["export_type"] == "rulesets"
        assert len(data["rulesets"]) == 2

    def test_exported_rules_preserve_order(self):
        rs_id, _ = _create_ruleset_with_rules()
        data = RulesExporter.export_ruleset(rs_id)
        rules = data["ruleset"]["rules"]
        assert rules[0]["order_index"] == 0
        assert rules[1]["order_index"] == 1


class TestRulesImporter:
    def test_import_rule(self):
        rule_data = {
            "rule": {
                "name": "Imported Rule",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "triggers": {"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
                "actions": {"action_0": {"action_type": ExpertActionType.BUY.value}},
                "continue_processing": False,
            }
        }
        rule_id, warnings = RulesImporter.import_rule(rule_data)
        assert rule_id > 0
        fetched = get_instance(EventAction, rule_id)
        assert fetched.name == "Imported Rule"

    def test_import_duplicate_rule_reuses_existing(self):
        rule_data = {
            "rule": {
                "name": "Reuse Test Rule",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "triggers": {},
                "actions": {},
                "continue_processing": False,
            }
        }
        id1, _ = RulesImporter.import_rule(rule_data)
        id2, warnings = RulesImporter.import_rule(rule_data)
        assert id1 == id2
        assert any("already exists" in w for w in warnings)

    def test_import_ruleset(self):
        ruleset_data = {
            "ruleset": {
                "name": "Imported Ruleset",
                "description": "Test import",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "subtype": None,
                "rules": [
                    {
                        "name": "Imported RS Rule 1",
                        "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                        "triggers": {"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
                        "actions": {"action_0": {"action_type": ExpertActionType.BUY.value}},
                        "continue_processing": False,
                        "order_index": 0,
                    }
                ],
            }
        }
        rs_id, warnings = RulesImporter.import_ruleset(ruleset_data)
        assert rs_id > 0
        fetched = get_instance(Ruleset, rs_id)
        assert fetched.name == "Imported Ruleset"

    def test_import_ruleset_duplicate_name_gets_suffixed(self):
        ruleset_data = {
            "ruleset": {
                "name": "Dup Name Ruleset",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "rules": [],
            }
        }
        id1, _ = RulesImporter.import_ruleset(ruleset_data)
        id2, warnings = RulesImporter.import_ruleset(ruleset_data)
        assert id1 != id2
        fetched2 = get_instance(Ruleset, id2)
        assert fetched2.name == "Dup Name Ruleset-1"

    def test_export_import_roundtrip(self):
        rs_id, _ = _create_ruleset_with_rules()
        exported = RulesExporter.export_ruleset(rs_id)
        imported_id, _ = RulesImporter.import_ruleset(exported)
        re_exported = RulesExporter.export_ruleset(imported_id)
        assert len(re_exported["ruleset"]["rules"]) == len(exported["ruleset"]["rules"])
