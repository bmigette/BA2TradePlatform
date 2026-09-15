"""ConditionLeaf mode-gene metadata (design 2026-09-15 §5 incl. amendment 2).

The three fields MUST be declared on ConditionLeaf: extra='allow' keeps unknown keys on the
model, but to_canonical_dict rebuilds from declared fields only, so an undeclared key would be
silently dropped by normalize_trade_rules.
"""
import pytest

from ba2_common.core.rule_models import (
    ConditionLeaf,
    NUMERIC_MODE_CHOICES,
    leaf_mode_kind,
    normalize_trade_rules,
)

_RANGE = dict(value_min=10, value_max=40, value_step=5)
_ADX = dict(id="o_ic-market-adx", field="underlying_adx_14", op="<", value=25, optimize=True, **_RANGE)
_CAT_CHOICES = ["off", "bull", "bear"]


def test_mode_metadata_round_trips_through_canonical_dict_in_both_spellings():
    leaf = ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "below", "above"])
    out = leaf.to_canonical_dict()
    assert out["mode_optimize"] is True and out["modeOptimize"] is True
    assert out["mode_choices"] == ["off", "below", "above"] and out["modeChoices"] == out["mode_choices"]
    assert "mode" not in out
    assert ConditionLeaf(**out).to_canonical_dict() == out
    camel = ConditionLeaf(**{"id": "x", "field": "underlying_adx_14", "op": "<", "value": 20,
                             "valueMin": 10, "valueMax": 40, "valueStep": 5, "modeOptimize": True,
                             "modeChoices": ["off", "below", "above"]})
    assert camel.mode_optimize is True and camel.mode_choices == ["off", "below", "above"]


def test_mode_and_toggle_optimize_together_is_rejected():
    with pytest.raises(ValueError) as ei:
        ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "below", "above"], toggle_optimize=True)
    msg = str(ei.value)
    assert "mode_optimize" in msg and "toggle_optimize" in msg and "o_ic-market-adx" in msg
    # camelCase spellings are rejected the same way
    with pytest.raises(ValueError):
        ConditionLeaf(**{**_ADX, "modeOptimize": True, "modeChoices": ["off", "below", "above"],
                         "toggleOptimize": True})
    # either one alone is fine
    ConditionLeaf(**_ADX, toggle_optimize=True)
    ConditionLeaf(**_ADX, mode_optimize=True, mode_choices=["off", "below", "above"], toggle_optimize=False)


def test_error_message_falls_back_to_field_without_id():
    with pytest.raises(ValueError, match="underlying_adx_14"):
        ConditionLeaf(field="underlying_adx_14", op="<", value=1, mode="sideways", **_RANGE)


@pytest.mark.parametrize("extra", [
    dict(mode="sideways"),
    dict(mode_optimize=True, mode_choices=["off", "above"]),
    dict(mode_optimize=True, mode_choices=["off", "above", "below"]),
    dict(mode_optimize=True),
    dict(mode_optimize=True, mode_choices=[]),
    dict(mode_optimize=True, mode_choices=["off", "below", "above"], mode="bull"),
    dict(mode_optimize=True, mode_choices=_CAT_CHOICES),
    dict(mode="bull"),
    dict(mode="none"),
], ids=["unknown-mode", "missing-below", "wrong-order", "no-choices", "empty-choices",
        "mode-outside-choices", "categorical-choices", "categorical-token", "none-token"])
def test_numeric_leaf_rejections(extra):
    with pytest.raises(ValueError) as ei:
        ConditionLeaf(**{**_ADX, "id": "numeric-bad-leaf"}, **extra)
    assert "numeric-bad-leaf" in str(ei.value)


@pytest.mark.parametrize("choices", [["off", "below"], ["off", "above", "below"], ["off", "below", "above", "x"]],
                         ids=["subset", "reordered", "superset"])
def test_numeric_choice_shape_is_checked_without_mode_optimize(choices):
    with pytest.raises(ValueError) as ei:
        ConditionLeaf(**{**_ADX, "id": "numeric-shape-leaf"}, mode_choices=choices)
    assert "numeric-shape-leaf" in str(ei.value)
    # the exact numeric list is fine without mode_optimize
    ConditionLeaf(**_ADX, mode_choices=list(NUMERIC_MODE_CHOICES))


def test_categorical_template_declares_off_plus_values_and_no_threshold():
    ok = ConditionLeaf(id="s1-structure-state", field="structure_state", op="==",
                       mode_optimize=True, mode_choices=_CAT_CHOICES)
    assert ok.to_canonical_dict()["mode_choices"] == _CAT_CHOICES


@pytest.mark.parametrize("bad", [
    dict(value=1, mode_optimize=True, mode_choices=_CAT_CHOICES),
    dict(mode_optimize=True, mode_choices=["off", "bull", "bear", "none"]),
    dict(mode_optimize=True, mode_choices=["bull", "off", "bear"]),
    dict(mode_optimize=True, mode_choices=["off", "bull", "bull"]),
    dict(mode_optimize=True, mode_choices=["off", "below", "bull"]),
    dict(mode_optimize=True, mode_choices=["off", "bull", "above"]),
    dict(mode_optimize=True, mode_choices=["off"]),
    dict(mode_choices=["off", "none"]),
], ids=["template-with-threshold", "none-choice", "off-not-first", "duplicates", "below-choice",
        "above-choice", "no-value-choice", "none-without-optimize"])
def test_categorical_template_rejections(bad):
    with pytest.raises(ValueError) as ei:
        ConditionLeaf(id="cat-bad-leaf", field="structure_state", op="==", **bad)
    assert "cat-bad-leaf" in str(ei.value)


def test_categorical_template_value_message_says_no_threshold():
    with pytest.raises(ValueError, match="no threshold"):
        ConditionLeaf(id="s", field="structure_state", op="==", value=1, mode_optimize=True,
                      mode_choices=_CAT_CHOICES)


def test_decoded_categorical_leaf_carries_its_code_as_value():
    kw = dict(id="s1-structure-state", field="structure_state", op="==", value=1.0,
              mode_optimize=True, mode_choices=_CAT_CHOICES)
    out = ConditionLeaf(**kw, mode="bull").to_canonical_dict()
    assert out["value"] == 1.0 and out["mode"] == "bull"
    assert out["op"] == "==" and out["comparison"] == "=="
    assert ConditionLeaf(**out).to_canonical_dict() == out
    for mode in ("off", None):
        with pytest.raises(ValueError, match="no threshold"):
            ConditionLeaf(**kw, mode=mode)


def test_deployed_categorical_leaf_with_stripped_metadata_is_accepted():
    # An exported/deployed leaf: the optimizer metadata is gone, only the resolved token and code
    # remain. The token is checked against the registry codes at collection/launch, not here.
    leaf = ConditionLeaf(**{"field": "s", "op": "==", "value": 1.0, "mode": "bull"})
    assert leaf.mode == "bull" and leaf._mode_kind() == "categorical"
    assert leaf.to_canonical_dict()["mode"] == "bull"


@pytest.mark.parametrize("choices", [None, _CAT_CHOICES], ids=["no-choices", "with-choices"])
@pytest.mark.parametrize("token", ["below", "above", "none"])
def test_categorical_leaf_rejects_numeric_and_forbidden_tokens(choices, token):
    with pytest.raises(ValueError) as ei:
        ConditionLeaf(id="cat-token-leaf", field="structure_state", op="==", value=1.0, mode=token,
                      mode_choices=choices)
    assert "cat-token-leaf" in str(ei.value)


def test_categorical_leaf_with_choices_rejects_token_outside_them():
    with pytest.raises(ValueError, match="cat-leaf"):
        ConditionLeaf(id="cat-leaf", field="structure_state", op="==", value=3.0, mode="range",
                      mode_choices=_CAT_CHOICES)


def test_resolved_mode_token_is_preserved_but_never_invented():
    below = ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=20, mode="below", **_RANGE)
    assert below.to_canonical_dict()["mode"] == "below"
    assert ConditionLeaf(**below.to_canonical_dict()).mode == "below"
    assert ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=20, mode="off", **_RANGE).mode == "off"
    plain = ConditionLeaf(id="c", field="confidence", op=">=", value=70).to_canonical_dict()
    for k in ("mode", "mode_optimize", "modeOptimize", "mode_choices", "modeChoices"):
        assert k not in plain
    cat = ConditionLeaf(id="s", field="structure_state", op="==", mode="bull", value=1.0,
                        mode_optimize=True, mode_choices=_CAT_CHOICES)
    assert cat.to_canonical_dict()["mode"] == "bull"


@pytest.mark.parametrize("leaf,expected", [
    ({**_ADX, "mode_optimize": True, "mode_choices": list(NUMERIC_MODE_CHOICES)}, "numeric"),
    ({"id": "s", "field": "structure_state", "op": "==", "modeOptimize": True, "modeChoices": _CAT_CHOICES},
     "categorical"),
    ({"field": "f", "modeOptimize": True, "modeChoices": list(NUMERIC_MODE_CHOICES), "valueMin": 1}, "numeric"),
    ({"field": "f", "modeOptimize": True, "modeChoices": list(NUMERIC_MODE_CHOICES), "valueOffsetFrom": "y"},
     "numeric"),
    ({"field": "f", "mode": "bull"}, "categorical"),
    ({"field": "f", "mode": "below", "value": 3, "valueStep": 1}, "numeric"),
    ({"field": "f", "mode": "below", "value": 3, "value_max": 5}, "numeric"),
    ({"field": "f", "mode": "bull", "value": 1.0, "mode_choices": _CAT_CHOICES}, "categorical"),
    ({"id": "c", "field": "confidence", "op": ">=", "value": 70}, None),
    ({"id": "c", "field": "confidence", "value": 70, "mode_optimize": False}, None),
    # (a) AliasChoices takes the FIRST PRESENT key even when it is None -> no range -> categorical
    ({"field": "f", "value_min": None, "valueMin": 10, "mode_choices": ["off", "bull"]}, "categorical"),
    # (b) "0" coerces to False -> not a mode leaf
    ({"field": "f", "modeOptimize": "0"}, None),
    # (c) declared choices alone make it a mode leaf
    ({"field": "f", "mode_choices": ["off", "bull"]}, "categorical"),
], ids=["numeric-template", "categorical-template", "camel-valueMin", "camel-offset", "mode-only",
        "camel-step", "snake-max", "decoded-categorical", "plain", "optimize-false",
        "alias-first-present-none", "string-zero", "choices-only"])
def test_leaf_mode_kind_agrees_with_the_model(leaf, expected):
    assert ConditionLeaf.model_validate(dict(leaf))._mode_kind() == expected
    assert leaf_mode_kind(leaf) == expected


def test_leaf_mode_kind_fails_loud_on_an_invalid_leaf():
    with pytest.raises(ValueError):
        leaf_mode_kind({"field": "f", "mode": "sideways", "value_min": 1})


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
