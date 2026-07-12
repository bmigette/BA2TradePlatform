"""Canonical TradeRule model (unified EventAction-shaped strategy rules) — normalization,
legacy lifts, and the legacy-trio conversion. See docs/plans/2026-07-08-unified-rule-model.md."""
from ba2_common.core.rule_models import (
    normalize_trade_rules,
    trade_rules_from_legacy,
)


def test_new_shape_rule_normalizes_actions_and_continue_flag():
    rules = [{
        "id": "r1", "name": "BUY_HighConf",
        "conditions": {"operator": "AND", "conditions": [
            {"field": "bullish", "fieldType": "flag"},
            {"field": "confidence", "comparison": "gte", "value": 80},
        ]},
        "actions": [
            {"action_type": "buy"},
            {"action": "adjust_take_profit", "referenceValue": "expert_target_price",
             "actionValue": -2, "actionValueOptimize": True,
             "actionValueMin": -20, "actionValueMax": 10, "actionValueStep": 2},
        ],
        "continueProcessing": True,
        "toggleOptimize": True,
    }]
    out = normalize_trade_rules(rules)
    assert len(out) == 1
    r = out[0]
    assert r["continue_processing"] is True and r["continueProcessing"] is True
    assert r["toggle_optimize"] is True
    # word-form comparison normalized to engine symbol
    leaf = r["conditions"]["conditions"][1]
    assert leaf["comparison"] == ">=" and leaf["op"] == ">="
    # actions keep order; both spellings + live 'value' mirror emitted
    a0, a1 = r["actions"]
    assert a0["action_type"] == "buy"
    assert a1["action_type"] == "adjust_take_profit"
    assert a1["reference_value"] == "expert_target_price"
    assert a1["action_value"] == -2 and a1["value"] == -2
    assert a1["action_value_min"] == -20 and a1["action_value_max"] == 10


def test_legacy_single_action_row_lifts_to_one_action_rule():
    """The historical exit_conditions / entry_actions shape (action fields at top level)
    lifts to a TradeRule with ONE action; option_* params travel with the action."""
    legacy = [{
        "id": "exit_belock", "action_type": "adjust_stop_loss",
        "reference_value": "order_open_price", "action_value": 4.0,
        "action_value_optimize": True, "action_value_min": 0.0, "action_value_max": 8.0,
        "action_value_step": 1.0, "toggle_optimize": True,
        "conditions": {"type": "AND", "conditions": [
            {"id": "xlk", "field": "profit_loss_percent", "op": ">", "value": 16}]},
        "option_strike_param": 0.3,
    }]
    out = normalize_trade_rules(legacy)
    r = out[0]
    assert r["id"] == "exit_belock"
    assert r["continue_processing"] is False  # legacy rows are first-match rows
    assert r["toggle_optimize"] is True       # rule-level toggle stays on the rule
    assert len(r["actions"]) == 1
    a = r["actions"][0]
    assert a["action_type"] == "adjust_stop_loss"
    assert a["action_value"] == 4.0
    assert a["option_strike_param"] == 0.3    # extras preserved on the action
    assert r["conditions"]["conditions"][0]["field"] == "profit_loss_percent"


def test_mixed_new_and_legacy_rows_in_one_list():
    out = normalize_trade_rules([
        {"id": "n", "actions": [{"action_type": "close"}], "conditions": None},
        {"id": "l", "action": "sell"},
        "not-a-dict",
    ])
    assert [r["id"] for r in out] == ["n", "l"]
    assert out[1]["actions"][0]["action_type"] == "sell"


def test_trade_rules_from_legacy_trio_replicates_bracket_per_branch():
    """buy tree with 2 OR branches + flat entry_actions -> 2 entry rules, EACH carrying
    open + bracket actions (the old seeder's replication made explicit), exits lifted."""
    buy_tree = {"operator": "OR", "conditions": [
        {"operator": "AND", "conditions": [{"field": "bullish", "fieldType": "flag"}]},
        {"operator": "AND", "conditions": [
            {"field": "confidence", "comparison": ">=", "value": 90}]},
    ]}
    entry_actions = [
        {"id": "tp", "action_type": "adjust_take_profit",
         "reference_value": "expert_target_price", "action_value": -2},
    ]
    exits = [{"id": "x1", "action_type": "close",
              "conditions": {"type": "AND", "conditions": [{"field": "bearish"}]}}]
    got = trade_rules_from_legacy(buy_tree=buy_tree, entry_actions=entry_actions,
                                  exit_conditions=exits)
    assert len(got["entry_rules"]) == 2
    for r in got["entry_rules"]:
        kinds = [a["action_type"] for a in r["actions"]]
        assert kinds == ["buy", "adjust_take_profit"]
        assert r["continue_processing"] is False
    assert [r["id"] for r in got["entry_rules"]] == ["buy-1", "buy-2"]
    # The old seeder's implicit base gates (bullish + has_no_position) become explicit
    # leaves — added only where the branch didn't already carry them.
    def fields(r):
        return {c["field"] for c in r["conditions"]["conditions"]}
    assert fields(got["entry_rules"][0]) == {"bullish", "has_no_position"}
    assert fields(got["entry_rules"][1]) == {"bullish", "has_no_position", "confidence"}
    assert len(got["exit_rules"]) == 1
    assert got["exit_rules"][0]["actions"][0]["action_type"] == "close"


def test_trade_rules_from_legacy_single_branch_and_none_trees():
    got = trade_rules_from_legacy(
        buy_tree={"operator": "AND", "conditions": [{"field": "bullish", "fieldType": "flag"}]},
    )
    assert [r["id"] for r in got["entry_rules"]] == ["buy"]
    assert got["exit_rules"] == []
    assert trade_rules_from_legacy()["entry_rules"] == []


def test_stale_camelcase_mirror_does_not_win_over_fresh_snake_case():
    """Regression for a real incident: a GA gene decode had mutated an action's snake_case
    ``action_value``/``value`` in place without refreshing the camelCase ``actionValue``
    mirror, leaving a persisted rule with TWO conflicting values for the same field. Because
    AliasChoices picks whichever alias is listed FIRST when several are present in the input,
    a camelCase-first order silently used the STALE value -- confirmed live: it corrupted an
    exported/deployed backtest rule's take-profit (50 instead of the real, GA-decoded 48) and
    stop-loss (2 instead of the real 1). snake_case/``value`` must win whenever both are
    present, since those are what the execution engine and the frontend's own read-back
    already treat as authoritative."""
    rules = [{
        "id": "tier3",
        "conditions": {"operator": "AND", "conditions": [
            {"field": "profit_loss_percent", "comparison": ">", "value": 24.0},
        ]},
        "actions": [
            {
                "id": "tier3_tp", "action_type": "adjust_take_profit",
                "reference_value": "order_open_price",
                # Conflicting spellings, as actually persisted in the incident: stale
                # camelCase (50.0) vs fresh snake_case + 'value' (48.0, the real GA gene).
                "actionValue": 50.0, "action_value": 48.0, "value": 48.0,
            },
        ],
    }]
    out = normalize_trade_rules(rules)
    assert out[0]["actions"][0]["action_value"] == 48.0
    assert out[0]["actions"][0]["actionValue"] == 48.0  # canonical output re-syncs both spellings
