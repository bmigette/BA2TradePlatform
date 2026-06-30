"""Cross-platform ruleset round-trip fidelity (live <-> test/backtest), via the SHARED modules.

Rule PROCESSING is already one path (both platforms seed EventActions and evaluate them with the
live TradeActionEvaluator). What differs is the AUTHORING/serialisation shape: the live platform
exports/imports EventAction ``export_type`` files; the test platform authors condition TREES
(optimizer genes / strategy_params). They are bridged by ba2_common.core.rules_convert
(re-exported via rule_builders). These tests pin that bridge LOSSLESS in BOTH directions so a
strategy moves between platforms without silent drift — guarding the bugs found 2026-06-29/30:
OR-of-groups collapsing into one ANDed rule, exit-action loss, and import-time cycles.
"""
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


def _buy(name, triggers):
    return {"name": name, "subtype": "enter_market",
            "triggers": triggers, "actions": {"a": {"action_type": "buy"}}}


# A realistic LIVE export: enter_market = 3 OR rules (each AND of flag + numeric triggers),
# open_positions = 2 exit rules (close + a numeric condition).
LIVE_EXPORT = {
    "export_type": "rulesets",
    "rulesets": [
        _enter_ruleset([
            _buy("A", {"t0": {"event_type": "bullish"},
                       "t1": {"event_type": "confidence", "operator": ">=", "value": 64},
                       "t2": {"event_type": "long_term"}}),
            _buy("B", {"t0": {"event_type": "bullish"},
                       "t1": {"event_type": "short_term"}}),
            _buy("C", {"t0": {"event_type": "bullish"},
                       "t1": {"event_type": "confidence", "operator": ">=", "value": 105}}),
        ]),
        _open_ruleset([
            {"name": "X1", "subtype": "open_positions",
             "triggers": {"t0": {"event_type": "has_position"},
                          "t1": {"event_type": "profit_loss_percent", "operator": ">", "value": 3}},
             "actions": {"a": {"action_type": "close"}}},
            {"name": "X2", "subtype": "open_positions",
             "triggers": {"t0": {"event_type": "has_position"},
                          "t1": {"event_type": "days_opened", "operator": ">=", "value": 48}},
             "actions": {"a": {"action_type": "close"}}},
        ]),
    ],
}


def _buy_branches(strategy):
    """Top-level OR branches of a strategy's buy tree (1 branch when AND/leaf)."""
    t = strategy["buy_entry_conditions"]
    if t is None:
        return []
    return t["conditions"] if str(t.get("operator", "AND")).upper() == "OR" else [t]


def _branch_fields(branch):
    return sorted(leaf["field"] for leaf in branch.get("conditions", [branch]))


def test_live_to_strategy_preserves_or_groups_and_exits():
    strat = live_export_to_strategy(LIVE_EXPORT)
    branches = _buy_branches(strat)
    assert len(branches) == 3                       # 3 enter rules -> 3 OR branches (NOT merged)
    sizes = sorted(len(b["conditions"]) for b in branches)
    assert sizes == [2, 2, 3]                       # B,C have 2 triggers; A has 3
    assert len(strat["exit_conditions"]) == 2
    assert all(x["action"] == "close" for x in strat["exit_conditions"])


def test_live_strategy_live_strategy_is_idempotent():
    """live -> strategy -> live -> strategy must be STABLE (no branch/exit drift across the bridge)."""
    s1 = live_export_to_strategy(LIVE_EXPORT)
    s2 = live_export_to_strategy(strategy_to_live_export(
        buy_tree=s1["buy_entry_conditions"], sell_tree=s1["sell_entry_conditions"],
        exit_rules=s1["exit_conditions"], name="rt"))
    b1, b2 = _buy_branches(s1), _buy_branches(s2)
    assert len(b1) == len(b2) == 3
    assert sorted(_branch_fields(b) for b in b1) == sorted(_branch_fields(b) for b in b2)
    assert [x["action"] for x in s1["exit_conditions"]] == [x["action"] for x in s2["exit_conditions"]]


def test_strategy_to_live_to_strategy_roundtrip_with_short_and_exits():
    """test-authored trees (multi-OR long + a short rule + exits) survive trees->live->trees."""
    buy = {"operator": "OR", "conditions": [
        {"operator": "AND", "conditions": [
            {"field": "bullish", "fieldType": "flag"},
            {"field": "confidence", "comparison": ">=", "value": 70, "fieldType": "number"}]},
        {"operator": "AND", "conditions": [
            {"field": "bullish", "fieldType": "flag"},
            {"field": "lowrisk", "fieldType": "flag"}]},
    ]}
    sell = {"operator": "AND", "conditions": [
        {"field": "bearish", "fieldType": "flag"},
        {"field": "highrisk", "fieldType": "flag"}]}
    exits = [{"name": "tp", "action": "close",
              "conditions": {"operator": "AND", "conditions": [
                  {"field": "profit_loss_percent", "comparison": ">", "value": 10, "fieldType": "number"}]}}]
    live = strategy_to_live_export(buy_tree=buy, sell_tree=sell, exit_rules=exits, name="s")
    # enter_market should carry 2 buy rules (OR) + 1 sell rule; open_positions 1 exit.
    enter = next(r for r in live["rulesets"] if r["subtype"] == "enter_market")
    assert len(enter["rules"]) == 3  # 2 buy branches + 1 sell
    again = live_export_to_strategy(live)
    assert len(_buy_branches(again)) == 2
    assert again["sell_entry_conditions"] is not None
    assert len(again["exit_conditions"]) == 1 and again["exit_conditions"][0]["action"] == "close"
