"""Tests for the shared condition-tree / exit-rule -> EventAction builder.

These pin the field-name reconciliation (API ``comparison``/``action``/``action_value``
vs seeding ``op``/``operator``/``action_type``/``value``) that the single shared core in
``ba2_common.core.rule_builders`` provides for both platforms + the API path.
"""
from ba2_common.core.rule_builders import (
    triggers_from_condition_tree,
    action_from_rule,
    FLAG_FIELD_EVENT,
    FIELD_EVENT,
)


def test_numeric_leaf_uses_comparison_as_operator():
    tree = {
        "id": "g",
        "operator": "AND",
        "conditions": [
            {"id": "c", "field": "profit_loss_percent", "comparison": ">=", "value": 5}
        ],
    }
    t = triggers_from_condition_tree(tree)
    (only,) = t.values()
    assert (
        only["event_type"] == "profit_loss_percent"
        and only["operator"] == ">="
        and only["value"] == 5
    )


def test_flag_leaf_value_less():
    tree = {"id": "g", "conditions": [{"id": "c", "field": "bearish"}]}
    (only,) = triggers_from_condition_tree(tree).values()
    assert only == {"event_type": "bearish"}


def test_action_alias_and_value_reconciliation():
    a = action_from_rule(
        {"action": "adjust_sl", "reference_value": "order_open_price", "action_value": -10}
    )
    assert (
        a["act"]["action_type"] == "adjust_stop_loss"
        and a["act"]["value"] == -10
        and a["act"]["reference_value"] == "order_open_price"
    )


def test_action_type_native_shape_still_works():
    a = action_from_rule({"action_type": "close"})
    assert a["act"] == {"action_type": "close"}


def test_unknown_action_returns_none():
    assert action_from_rule({"action": "frobnicate"}) is None


def test_option_action_carries_selection_params_in_evaluator_keys():
    """An option exit/entry rule must emit an action config in the EXACT shape the
    ``TradeActionEvaluator`` reads (strike_method/strike_param/dte_min/dte_max/sizing +
    liquidity), so the backtest builds the option TradeAction identically to live."""
    a = action_from_rule(
        {
            "action": "buy_call",
            "option_strike_method": "delta",
            "option_strike_param": 0.3,
            "option_dte_min": 20,
            "option_dte_max": 45,
            "option_sizing": 5,
        }
    )
    cfg = a["act"]
    assert cfg["action_type"] == "buy_call"
    assert cfg["strike_method"] == "delta"
    assert cfg["strike_param"] == 0.3
    assert cfg["dte_min"] == 20
    assert cfg["dte_max"] == 45
    assert cfg["sizing"] == 5
    # No liquidity fields were provided, so they must be absent (action defaults apply).
    assert "min_open_interest" not in cfg
    assert "max_spread_pct" not in cfg


def test_option_action_type_native_shape_and_liquidity():
    """The seeding shape (``action_type``) and liquidity aliases (option_min_oi /
    option_max_spread_pct) also resolve to the evaluator's keys."""
    a = action_from_rule(
        {
            "action_type": "open_bull_call_spread",
            "option_strike_method": "percent_otm",
            "option_strike_param": 2.5,
            "option_min_oi": 100,
            "option_max_spread_pct": 0.15,
        }
    )
    cfg = a["act"]
    assert cfg["action_type"] == "open_bull_call_spread"
    assert cfg["strike_method"] == "percent_otm"
    assert cfg["strike_param"] == 2.5
    assert cfg["min_open_interest"] == 100
    assert cfg["max_spread_pct"] == 0.15
    # dte/sizing omitted -> absent
    assert "dte_min" not in cfg
    assert "sizing" not in cfg


def test_close_option_action_carries_no_selection_params():
    """``close_option`` resolves the contract from the held position, so it must NOT carry
    strike/dte/sizing params (mirrors live, where CLOSE_OPTION takes none)."""
    a = action_from_rule({"action": "close_option"})
    assert a["act"] == {"action_type": "close_option"}


# ---------------------------------------------------------------------------
# action_type_of / rule_carries_action -- "which action does this rule carry?"
#
# The live option lifecycle pass asks this to tell "the rule will roll it" from "nothing
# will roll it", so a mis-read here turns a loud refusal into a false all-clear.
# ---------------------------------------------------------------------------
def test_both_spellings_of_the_action_type_are_read():
    """``action_type`` is what the builders write; ``type`` is the older API/UI spelling
    still present in seeded rulesets. Reading one silently mis-reads half the rows."""
    from ba2_common.core.rule_builders import action_type_of

    assert action_type_of({"action_type": "roll_pmcc_short"}) == "roll_pmcc_short"
    assert action_type_of({"type": "roll_pmcc_short"}) == "roll_pmcc_short"


def test_action_type_prefers_action_type_and_falls_through_an_empty_one():
    """Falsy-first, exactly as the inline reads it replaces."""
    from ba2_common.core.rule_builders import action_type_of

    assert action_type_of({"action_type": "close_option", "type": "buy"}) == "close_option"
    assert action_type_of({"action_type": "", "type": "buy"}) == "buy"


def test_an_entry_that_declares_no_action_answers_None_rather_than_raising():
    from ba2_common.core.rule_builders import action_type_of

    assert action_type_of({}) is None
    assert action_type_of({"value": 3}) is None
    assert action_type_of(None) is None
    assert action_type_of("roll_pmcc_short") is None


def test_a_rule_carries_an_action_regardless_of_its_slot_name():
    """``EventAction.actions`` is keyed by an arbitrary slot ("act", "act2", ...), so the
    answer is over its VALUES -- a search over the keys would find nothing."""
    from ba2_common.core.rule_builders import rule_carries_action

    actions = {"act": {"action_type": "close_option"},
               "act2": {"action_type": "roll_pmcc_short"}}
    assert rule_carries_action(actions, "roll_pmcc_short") is True
    assert rule_carries_action(actions, "close_option") is True
    assert rule_carries_action(actions, "sell_covered_call") is False


def test_a_rule_with_no_actions_carries_nothing():
    from ba2_common.core.rule_builders import rule_carries_action

    assert rule_carries_action({}, "roll_pmcc_short") is False
    assert rule_carries_action(None, "roll_pmcc_short") is False


def test_the_builder_and_the_reader_agree_on_the_shape():
    """END TO END over the one shape that matters: what ``action_from_rule`` WRITES is what
    ``rule_carries_action`` READS. Two functions in this module disagreeing about the key
    would make the live roll-ownership check answer about a shape nothing emits."""
    from ba2_common.core.rule_builders import action_from_rule, rule_carries_action

    built = action_from_rule({"action": "roll_pmcc_short"})
    assert rule_carries_action(built, "roll_pmcc_short") is True
