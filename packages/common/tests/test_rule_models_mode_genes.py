"""ConditionLeaf mode-gene metadata (design 2026-09-15 §5 incl. amendment 2).

The three fields MUST be declared on ConditionLeaf: extra='allow' keeps unknown keys on the
model, but to_canonical_dict rebuilds from declared fields only, so an undeclared key would be
silently dropped by normalize_trade_rules.
"""
import pytest

from ba2_common.core.rule_models import (
    ConditionLeaf,
    FORBIDDEN_MODE_CHOICES,
    MODE_OFF,
    NUMERIC_MODE_CHOICES,
    leaf_mode_kind,
    normalize_trade_rules,
)

_ADX = dict(id="o_ic-market-adx", field="underlying_adx_14", op="<", value=25, optimize=True,
            value_min=10, value_max=40, value_step=5)


def test_constants():
    assert MODE_OFF == "off"
    assert NUMERIC_MODE_CHOICES == ("off", "below", "above")
    assert FORBIDDEN_MODE_CHOICES == ("none",)


def test_mode_metadata_round_trips_through_canonical_dict_in_both_spellings():
    leaf = ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "below", "above"])
    out = leaf.to_canonical_dict()
    assert out["mode_optimize"] is True and out["modeOptimize"] is True
    assert out["mode_choices"] == ["off", "below", "above"] and out["modeChoices"] == out["mode_choices"]
    assert "mode" not in out
    assert ConditionLeaf(**out).to_canonical_dict() == out
    camel = ConditionLeaf(**{"id": "x", "field": "underlying_adx_14", "op": "<", "value": 1,
                             "modeOptimize": True, "modeChoices": ["off", "below", "above"]})
    assert camel.mode_optimize is True and camel.mode_choices == ["off", "below", "above"]


def test_mode_and_toggle_optimize_together_is_rejected():
    with pytest.raises(ValueError) as ei:
        ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "below", "above"], toggle_optimize=True)
    msg = str(ei.value)
    assert "mode_optimize" in msg and "toggle_optimize" in msg
    # camelCase spellings are rejected the same way
    with pytest.raises(ValueError):
        ConditionLeaf(**{**_ADX, "modeOptimize": True, "modeChoices": ["off", "below", "above"],
                         "toggleOptimize": True})
    # either one alone is fine
    ConditionLeaf(**_ADX, toggle_optimize=True)
    ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "below", "above"], toggle_optimize=False)


def test_unknown_mode_value_and_unsupported_choice_list_are_rejected():
    with pytest.raises(ValueError):
        ConditionLeaf(**_ADX, mode="sideways")
    with pytest.raises(ValueError):
        ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "above"])
    with pytest.raises(ValueError):
        ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "above", "below"])
    with pytest.raises(ValueError):
        ConditionLeaf(**_ADX, mode_optimize=True)
    with pytest.raises(ValueError):
        ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=[])
    with pytest.raises(ValueError):   # declared choices but a resolved mode outside them
        ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "below", "above"], mode="bull")


def test_categorical_leaf_declares_off_plus_values_and_no_threshold():
    ok = ConditionLeaf(id="s1-structure-state", field="structure_state", op="==",
                       mode_optimize=True, mode_choices=["off", "bull", "bear"])
    assert ok.to_canonical_dict()["mode_choices"] == ["off", "bull", "bear"]
    for bad in (dict(value=1, mode_optimize=True, mode_choices=["off", "bull", "bear"]),    # threshold
                dict(value_min=1, mode_optimize=True, mode_choices=["off", "bull", "bear"]),
                dict(value_offset_from="y", mode_optimize=True, mode_choices=["off", "bull", "bear"]),
                dict(mode_optimize=True, mode_choices=["off", "bull", "bear", "none"]),       # none
                dict(mode_optimize=True, mode_choices=["bull", "off", "bear"]),               # first off
                dict(mode_optimize=True, mode_choices=["off", "bull", "bull"]),               # duplicates
                dict(mode_optimize=True, mode_choices=["off", "below", "bull"]),              # below on cat
                dict(mode_optimize=True, mode_choices=["off", "bull", "above"]),              # above on cat
                dict(mode_optimize=True, mode_choices=["off"])):                              # no value
        with pytest.raises(ValueError):
            ConditionLeaf(id="x", field="structure_state", op="==", **bad)
    with pytest.raises(ValueError):   # numeric leaf with categorical choices
        ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=1, mode_optimize=True,
                      mode_choices=["off", "bull", "bear"])
    with pytest.raises(ValueError):   # "none" is forbidden even without mode_optimize
        ConditionLeaf(id="x", field="structure_state", op="==", mode_choices=["off", "none"])


def test_resolved_mode_token_is_preserved_but_never_invented():
    below = ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=20, mode="below")
    assert below.to_canonical_dict()["mode"] == "below"
    assert ConditionLeaf(**below.to_canonical_dict()).mode == "below"
    assert ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=20, mode="off").mode == "off"
    plain = ConditionLeaf(id="c", field="confidence", op=">=", value=70).to_canonical_dict()
    for k in ("mode", "mode_optimize", "modeOptimize", "mode_choices", "modeChoices"):
        assert k not in plain
    cat = ConditionLeaf(id="s", field="structure_state", op="==", mode="bull",
                        mode_optimize=True, mode_choices=["off", "bull", "bear"])
    assert cat.to_canonical_dict()["mode"] == "bull"
    with pytest.raises(ValueError):
        ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=20, mode="bull")
    with pytest.raises(ValueError):
        ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=20, mode="none")


def test_leaf_mode_kind_helper():
    assert leaf_mode_kind({**_ADX, "mode_optimize": True, "mode_choices": list(NUMERIC_MODE_CHOICES)}) == "numeric"
    assert leaf_mode_kind({"id": "s", "field": "structure_state", "op": "==", "modeOptimize": True,
                           "modeChoices": ["off", "bull", "bear"]}) == "categorical"
    assert leaf_mode_kind({"id": "x", "field": "f", "modeOptimize": True, "valueMin": 1}) == "numeric"
    assert leaf_mode_kind({"id": "x", "field": "f", "modeOptimize": True, "valueOffsetFrom": "y"}) == "numeric"
    assert leaf_mode_kind({"id": "x", "field": "f", "mode": "bull"}) == "categorical"
    assert leaf_mode_kind({"id": "x", "field": "f", "mode": "below", "value": 3}) == "numeric"
    assert leaf_mode_kind({"id": "c", "field": "confidence", "op": ">=", "value": 70}) is None
    assert leaf_mode_kind({"id": "c", "field": "confidence", "value": 70, "mode_optimize": False}) is None


def test_legacy_leaf_canonical_output_is_unchanged():
    # Literals captured from HEAD c69b9a64 (before the mode fields existed).
    assert ConditionLeaf(id="c1", field="confidence", op=">=", value=70, optimize=True, value_min=50,
                         value_max=90, value_step=5, toggle_optimize=True,
                         confirmation_bars=2).to_canonical_dict() == {
        'id': 'c1', 'field': 'confidence', 'fieldType': 'numeric', 'field_type': 'numeric',
        'comparison': '>=', 'op': '>=', 'optimizeEnabled': True, 'optimize': True, 'value': 70.0,
        'valueMin': 50.0, 'value_min': 50.0, 'valueMax': 90.0, 'value_max': 90.0, 'valueStep': 5.0,
        'value_step': 5.0, 'toggleOptimize': True, 'toggle_optimize': True, 'confirmationBars': 2,
        'confirmation_bars': 2}
    assert ConditionLeaf(id="f1", field="has_no_position").to_canonical_dict() == {
        'id': 'f1', 'field': 'has_no_position', 'fieldType': 'flag', 'field_type': 'flag',
        'comparison': 'is_true', 'op': 'is_true', 'optimizeEnabled': False, 'optimize': False}
    assert ConditionLeaf(**{'id': 'w', 'field': 'expected_profit_percent', 'comparison': 'gte', 'value': 5,
                            'valueOffsetFrom': 'c1', 'valueMin': 1, 'valueMax': 3,
                            'valueStep': 1}).to_canonical_dict() == {
        'id': 'w', 'field': 'expected_profit_percent', 'fieldType': 'numeric', 'field_type': 'numeric',
        'comparison': '>=', 'op': '>=', 'optimizeEnabled': False, 'optimize': False, 'value': 5.0,
        'valueMin': 1.0, 'value_min': 1.0, 'valueMax': 3.0, 'value_max': 3.0, 'valueStep': 1.0,
        'value_step': 1.0, 'valueOffsetFrom': 'c1', 'value_offset_from': 'c1'}


def test_normalize_trade_rules_keeps_mode_metadata():
    rules = [{
        "id": "o_ic", "name": "enter",
        "conditions": {"id": "g", "operator": "AND", "conditions": [
            {"id": "o_ic-flat", "field": "has_no_position"},
            {**_ADX, "modeOptimize": True, "modeChoices": ["off", "below", "above"]},
        ]},
        "actions": [{"action_type": "buy"}],
    }]
    out = normalize_trade_rules(rules)
    leaf = out[0]["conditions"]["conditions"][1]
    assert leaf["mode_optimize"] is True and leaf["modeOptimize"] is True
    assert leaf["mode_choices"] == ["off", "below", "above"] and leaf["modeChoices"] == ["off", "below", "above"]
    assert normalize_trade_rules(out) == out
