"""The buy/sell CLOSE PERCENT (the action's ``action_value``/``value``) survives every converter.

A sell closing a long / a buy covering a short may close only a percent of the position (1..100,
absent = a full close). The value lives in the live EventAction as ``value`` (what
``TradeActionEvaluator._create_trade_action`` reads) and in a TradeRule as ``action_value``.
Every hop that could silently drop it is pinned here, and so is the byte-identity of a buy/sell
WITHOUT a percent (every stored rule), which must convert exactly as before.
"""
import pytest

from ba2_common.core.rule_builders import action_from_rule
from ba2_common.core.rule_models import normalize_trade_rules
from ba2_common.core.rules_convert import (
    _action_cfg_to_live, eventaction_to_exit_rule, live_actions_from_trade_rule,
    live_export_to_trade_rules, trade_rules_to_live_export,
)


class TestTreeFormBuilder:
    def test_a_sell_carries_its_percent(self):
        assert action_from_rule({"action_type": "sell", "action_value": 50}, key="k") == {
            "k": {"action_type": "sell", "value": 50}}
        assert action_from_rule({"action": "sell", "value": 25}, key="k") == {
            "k": {"action_type": "sell", "value": 25}}

    def test_a_sell_without_a_percent_is_unchanged(self):
        assert action_from_rule({"action_type": "sell"}, key="k") == {"k": {"action_type": "sell"}}

    def test_close_takes_no_value(self):
        assert action_from_rule({"action_type": "close", "action_value": 50}, key="k") == {
            "k": {"action_type": "close"}}


class TestUnifiedTradeRules:
    @pytest.mark.parametrize("at", ["buy", "sell"])
    def test_the_percent_reaches_the_live_action(self, at):
        assert _action_cfg_to_live({"action_type": at, "action_value": 40.0}, "a0") == {
            "a0": {"action_type": at, "value": 40.0}}

    @pytest.mark.parametrize("at", ["buy", "sell"])
    def test_no_percent_is_byte_identical(self, at):
        assert _action_cfg_to_live({"action_type": at}, "a0") == {"a0": {"action_type": at}}

    def test_the_seeder_path_carries_it(self):
        """live_actions_from_trade_rule is shared by the backtest seeder and the live export."""
        rule = normalize_trade_rules([{
            "id": "r", "conditions": {"operator": "AND", "conditions": []},
            "actions": [{"action_type": "sell", "action_value": 50},
                        {"action_type": "adjust_stop_loss", "reference_value": "order_open_price",
                         "action_value": -8}]}])[0]
        acts = live_actions_from_trade_rule(rule)
        assert acts["a0"] == {"action_type": "sell", "value": 50.0}

    def test_live_to_trade_rules_to_live_round_trip(self):
        payload = {"export_type": "rulesets", "rulesets": [{
            "name": "exit", "subtype": "open_positions", "rules": [{
                "name": "half-out", "subtype": "open_positions", "order_index": 0,
                "triggers": {"t0": {"event_type": "bearish"}},
                "actions": {"a0": {"action_type": "sell", "value": 50.0}},
                "continue_processing": False}]}]}
        rules = live_export_to_trade_rules(payload)
        [exit_rule] = rules["exit_rules"]
        assert exit_rule["actions"][0]["action_value"] == 50.0
        back = trade_rules_to_live_export(entry_rules=[], exit_rules=rules["exit_rules"])
        live_actions = [r["actions"] for rs in back["rulesets"] for r in rs["rules"]]
        assert {"action_type": "sell", "value": 50.0} in [a["a0"] for a in live_actions]


class TestLegacyReverse:
    def test_a_live_sell_exit_keeps_its_percent(self):
        rule = eventaction_to_exit_rule(
            1, "half-out", {"t0": {"event_type": "bearish"}},
            {"a0": {"action_type": "sell", "value": 30.0}})
        assert rule["action"] == "sell" and rule["action_value"] == 30.0

    def test_a_live_sell_exit_without_one_is_unchanged(self):
        rule = eventaction_to_exit_rule(
            1, "out", {"t0": {"event_type": "bearish"}}, {"a0": {"action_type": "sell"}})
        assert "action_value" not in rule
