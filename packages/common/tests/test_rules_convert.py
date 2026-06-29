"""Tests for ba2_common.core.rules_convert — live ruleset export-file <-> backtester shapes."""
from ba2_common.core.rule_builders import (
    live_export_to_strategy,
    strategy_to_live_export,
)


def _enter_ruleset(rules):
    return {"name": "enter", "type": "trading_recommendation_rule",
            "subtype": "enter_market", "rules": rules}


def _open_ruleset(rules):
    return {"name": "open", "type": "trading_recommendation_rule",
            "subtype": "open_positions", "rules": rules}


def test_enter_market_routes_buy_and_skips_stop_processing():
    payload = {
        "export_type": "rulesets",
        "rulesets": [_enter_ruleset([
            {"name": "STOP", "subtype": "enter_market",
             "triggers": {"t0": {"event_type": "percent_to_new_target", "operator": "<=", "value": 5}},
             "actions": {"a0": {"action_type": "stop_processing"}}},
            {"name": "BUY", "subtype": "enter_market",
             "triggers": {
                 "t0": {"event_type": "bullish"},
                 "t1": {"event_type": "confidence", "operator": ">=", "value": 80},
                 "t2": {"event_type": "long_term"},
                 "t3": {"event_type": "lowrisk"},
                 "t4": {"event_type": "has_no_position"},
             },
             "actions": {
                 "a0": {"action_type": "buy"},
                 "a1": {"action_type": "adjust_take_profit", "value": -5, "reference_value": "expert_target_price"},
                 "a2": {"action_type": "adjust_stop_loss", "value": -12, "reference_value": "order_open_price"},
             }},
        ])],
    }
    out = live_export_to_strategy(payload)
    buy = out["buy_entry_conditions"]
    assert buy is not None and buy["operator"] == "AND"
    fields = [leaf["field"] for leaf in buy["conditions"]]
    # Raw enum values used as field (not lossy inverse-map names).
    assert fields == ["bullish", "confidence", "long_term", "lowrisk", "has_no_position"]
    assert out["sell_entry_conditions"] is None
    assert out["exit_conditions"] == []
    assert out["summary"]["skipped_rules"] == 1          # the stop_processing rule
    assert out["summary"]["ignored_initial_brackets"] == 2  # the adjust_tp + adjust_sl


def test_numeric_enum_not_in_field_event_is_preserved():
    # percent_to_new_target is a valid ExpertEventType but NOT in FIELD_EVENT; the old lossy
    # entry path dropped it. Routed via a buy action it must survive as a raw-enum-value field.
    payload = {"export_type": "ruleset", "ruleset": _enter_ruleset([
        {"name": "BUY", "subtype": "enter_market",
         "triggers": {"t0": {"event_type": "percent_to_new_target", "operator": ">", "value": 3}},
         "actions": {"a0": {"action_type": "buy"}}},
    ])}
    out = live_export_to_strategy(payload)
    leaves = out["buy_entry_conditions"]["conditions"]
    assert leaves[0]["field"] == "percent_to_new_target"
    assert leaves[0]["field_type"] == "numeric"
    assert leaves[0]["comparison"] == ">"


def test_short_rules_route_to_sell_tree():
    payload = {"export_type": "rulesets", "rulesets": [_enter_ruleset([
        {"name": "SELL", "subtype": "enter_market",
         "triggers": {"t0": {"event_type": "bearish"}},
         "actions": {"a0": {"action_type": "sell"}}},
    ])]}
    out = live_export_to_strategy(payload)
    assert out["buy_entry_conditions"] is None
    assert out["sell_entry_conditions"] is not None
    assert out["summary"]["sell_rules"] == 1


def test_multiple_buy_rules_or_combined():
    payload = {"export_type": "rulesets", "rulesets": [_enter_ruleset([
        {"name": "B1", "subtype": "enter_market",
         "triggers": {"t0": {"event_type": "confidence", "operator": ">=", "value": 80}},
         "actions": {"a0": {"action_type": "buy"}}},
        {"name": "B2", "subtype": "enter_market",
         "triggers": {"t0": {"event_type": "confidence", "operator": ">=", "value": 70}},
         "actions": {"a0": {"action_type": "buy"}}},
    ])]}
    out = live_export_to_strategy(payload)
    buy = out["buy_entry_conditions"]
    assert buy["operator"] == "OR" and len(buy["conditions"]) == 2


def test_open_positions_to_exit_rules():
    payload = {"export_type": "rulesets", "rulesets": [_open_ruleset([
        {"name": "TakeProfit", "subtype": "open_positions",
         "triggers": {"t0": {"event_type": "profit_loss_percent", "operator": ">", "value": 10}},
         "actions": {"a0": {"action_type": "close"}}},
        {"name": "TrailStop", "subtype": "open_positions",
         "triggers": {"t0": {"event_type": "days_opened", "operator": ">=", "value": 5}},
         "actions": {"a0": {"action_type": "adjust_stop_loss", "value": -3, "reference_value": "current_price"}}},
    ])]}
    out = live_export_to_strategy(payload)
    rules = out["exit_conditions"]
    assert len(rules) == 2
    assert rules[0]["action"] == "close"
    assert rules[0]["conditions"]["conditions"][0]["field"] == "profit_loss_percent"
    assert rules[1]["action"] == "adjust_stop_loss"
    assert rules[1]["reference_value"] == "current_price"
    assert rules[1]["action_value"] == -3


def test_single_rule_flavor():
    payload = {"export_type": "rule", "rule": {
        "name": "BUY", "subtype": "enter_market",
        "triggers": {"t0": {"event_type": "bullish"}},
        "actions": {"a0": {"action_type": "buy"}},
    }}
    out = live_export_to_strategy(payload)
    assert out["buy_entry_conditions"] is not None
    assert out["summary"]["rules"] == 1


def test_strategy_to_live_export_roundtrips():
    src = {"export_type": "rulesets", "rulesets": [
        _enter_ruleset([
            {"name": "BUY", "subtype": "enter_market",
             "triggers": {
                 "t0": {"event_type": "bullish"},
                 "t1": {"event_type": "confidence", "operator": ">=", "value": 80},
             },
             "actions": {"a0": {"action_type": "buy"}}},
        ]),
        _open_ruleset([
            {"name": "Close", "subtype": "open_positions",
             "triggers": {"t0": {"event_type": "profit_loss_percent", "operator": ">", "value": 10}},
             "actions": {"a0": {"action_type": "close"}}},
        ]),
    ]}
    strat = live_export_to_strategy(src)
    exported = strategy_to_live_export(
        buy_tree=strat["buy_entry_conditions"],
        sell_tree=strat["sell_entry_conditions"],
        exit_rules=strat["exit_conditions"],
    )
    # Round-trip the exported file back to strategy shapes and confirm the buy leaves survive.
    again = live_export_to_strategy(exported)
    fields = [leaf["field"] for leaf in again["buy_entry_conditions"]["conditions"]]
    assert "bullish" in fields and "confidence" in fields
    assert again["exit_conditions"][0]["action"] == "close"


def test_strategy_to_live_export_splits_or_branches_into_rules():
    """A top-level OR of N AND-groups must export as N separate enter rules (live ORs rules within a
    ruleset). Regression: it used to collapse into ONE rule with every trigger ANDed -> never
    matched."""
    buy_tree = {
        "operator": "OR",
        "conditions": [
            {"operator": "AND", "conditions": [
                {"field": "bullish", "fieldType": "flag"},
                {"field": "confidence", "comparison": ">=", "value": 64, "fieldType": "number"},
                {"field": "long_term", "fieldType": "flag"},
            ]},
            {"operator": "AND", "conditions": [
                {"field": "bullish", "fieldType": "flag"},
                {"field": "short_term", "fieldType": "flag"},
            ]},
        ],
    }
    exported = strategy_to_live_export(buy_tree=buy_tree, sell_tree=None, exit_rules=[], name="s")
    enter = next(rs for rs in exported["rulesets"] if rs["subtype"] == "enter_market")
    assert len(enter["rules"]) == 2  # one rule per OR branch, NOT one mega-ANDed rule
    # branch 1 has 3 ANDed triggers, branch 2 has 2 — never merged
    assert {len(r["triggers"]) for r in enter["rules"]} == {3, 2}
    # round-trips back to an OR with 2 branches
    again = live_export_to_strategy(exported)
    assert again["buy_entry_conditions"]["operator"] == "OR"
    assert len(again["buy_entry_conditions"]["conditions"]) == 2


def test_unknown_event_type_does_not_raise():
    payload = {"export_type": "rule", "rule": {
        "name": "BUY", "subtype": "enter_market",
        "triggers": {"t0": {"event_type": "totally_made_up"}, "t1": {"event_type": "bullish"}},
        "actions": {"a0": {"action_type": "buy"}},
    }}
    out = live_export_to_strategy(payload)
    # The unknown trigger is skipped; the known one survives.
    fields = [leaf["field"] for leaf in out["buy_entry_conditions"]["conditions"]]
    assert fields == ["bullish"]
