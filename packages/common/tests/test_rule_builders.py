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
