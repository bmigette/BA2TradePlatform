"""``cond:<id>:mode`` gene: collection and decode (design 2026-09-15 §5 incl. amendment 2).

| mode              | concrete rule                                         |
|-------------------|-------------------------------------------------------|
| off               | leaf removed (never evaluated, even on missing data)  |
| below / above     | numeric leaf ``< threshold`` / ``> threshold``        |
| <choice> (categ.) | ``== code(choice)``; no threshold gene                |

GeneticOptimizer chromosome-encodes a ``type: "choice"`` gene as an int INDEX and
``decode_individual`` maps it back to the TOKEN, so ``decode_params`` normally receives the
token; a raw index (e.g. a hand-built flat dict) is also accepted and resolved through the
template leaf's own ``mode_choices``.
"""
import copy
import json
import types

import pytest

from ba2_common.core.market_conditions import FieldSpec, ProfileSpec, registered_profile
import app.services.strategy_param_space as sps
from app.services.genetic import GeneticOptimizer
from app.services.strategy_param_space import collect_param_space, decode_params

_STATE_FIELD = "test_market_state"
_STATE_CODES = {"bear": 2, "bull": 1, "chop": 3}  # insertion order != code order on purpose
_STATE_ORDER = ["bull", "bear", "chop"]            # ascending CODE order


@pytest.fixture
def state_field():
    spec = ProfileSpec(name="test-state-v1", calc_version="test/1", fields=(
        FieldSpec(name=_STATE_FIELD, kind="categorical", short="state", searched=True,
                  codes=_STATE_CODES, ui_name="Test market state"),
    ))
    with registered_profile(spec):
        yield spec


def _numeric_leaf(**over):
    leaf = {"id": "o_ic-market-adx", "field": "underlying_adx_14", "op": "<", "comparison": "<",
            "value": 25.0, "optimize": True, "value_min": 10.0, "value_max": 40.0,
            "value_step": 5.0, "mode_optimize": True, "mode_choices": ["off", "below", "above"]}
    leaf.update(over)
    return leaf


def _cat_leaf(**over):
    leaf = {"id": "o_ic-market-state", "field": _STATE_FIELD, "op": "==", "comparison": "==",
            "mode_optimize": True, "mode_choices": ["off", *_STATE_ORDER]}
    leaf.update(over)
    return leaf


def _base_leaf():
    return {"id": "base", "field": "rsi_14", "comparison": "<", "value": 30.0}


def _strategy(*leaves, with_base=True):
    kids = ([_base_leaf()] if with_base else []) + [copy.deepcopy(lf) for lf in leaves]
    rules = [{"id": "o_ic-entry", "continue_processing": False,
              "actions": [{"action_type": "buy"}],
              "conditions": {"operator": "AND", "conditions": kids}}]
    return types.SimpleNamespace(entry_rules=rules, exit_rules=[])


def _leaves(decoded):
    return decoded["entry_rules"][0]["conditions"]["conditions"]


def _by_id(decoded):
    return {lf["id"]: lf for lf in _leaves(decoded)}


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------
def test_mode_optimize_emits_a_choice_gene_next_to_the_value_gene():
    space = collect_param_space(_strategy(_numeric_leaf()))
    assert space["cond:o_ic-market-adx:value"] == {"type": "float", "min": 10.0, "max": 40.0,
                                                   "step": 5.0}
    assert space["cond:o_ic-market-adx:mode"] == {
        "type": "choice", "choices": ["off", "below", "above"], "min": 0, "max": 2, "step": 1}


def test_three_numeric_leaves_produce_six_genes():
    leaves = [_numeric_leaf(id=f"o_ic-market-{s}") for s in ("slope", "adx", "rv")]
    space = collect_param_space(_strategy(*leaves))
    assert sorted(space) == sorted(f"cond:o_ic-market-{s}:{g}"
                                   for s in ("slope", "adx", "rv") for g in ("value", "mode"))


def test_categorical_leaf_emits_only_a_mode_gene_with_off_then_codes_in_code_order(state_field):
    space = collect_param_space(_strategy(_cat_leaf()))
    assert list(space) == ["cond:o_ic-market-state:mode"]
    assert space["cond:o_ic-market-state:mode"] == {
        "type": "choice", "choices": ["off", "bull", "bear", "chop"], "min": 0, "max": 3,
        "step": 1}
    assert "cond:o_ic-market-state:value" not in space


def test_categorical_choices_must_match_the_registry_and_unknown_field_raises(state_field):
    with pytest.raises(ValueError, match="o_ic-market-state"):
        collect_param_space(_strategy(_cat_leaf(mode_choices=["off", "bear", "bull", "chop"])))
    with pytest.raises(ValueError, match="o_ic-market-state"):
        collect_param_space(_strategy(_cat_leaf(mode_choices=["off", "bull", "bear"])))
    with pytest.raises(KeyError, match="o_ic-market-state"):
        collect_param_space(_strategy(_cat_leaf(field="no_such_market_field")))


def test_mode_and_toggle_together_raise_at_collection():
    with pytest.raises(ValueError, match="o_ic-market-adx"):
        collect_param_space(_strategy(_numeric_leaf(toggle_optimize=True)))


def test_numeric_choice_list_other_than_off_below_above_raises():
    with pytest.raises(ValueError, match="o_ic-market-adx"):
        collect_param_space(_strategy(_numeric_leaf(mode_choices=["off", "above", "below"])))
    with pytest.raises(ValueError, match="o_ic-market-adx"):
        collect_param_space(_strategy(_numeric_leaf(mode_choices=["off", "below"])))


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------
def test_decode_off_drops_the_leaf_from_the_tree():
    s = _strategy(_numeric_leaf())
    out = decode_params(s, {"cond:o_ic-market-adx:mode": "off",
                            "cond:o_ic-market-adx:value": 35.0})
    assert [lf["id"] for lf in _leaves(out)] == ["base"]


def test_decode_below_and_above_set_both_op_and_comparison_and_keep_mode():
    s = _strategy(_numeric_leaf(op=">", comparison=">"))
    below = _by_id(decode_params(s, {"cond:o_ic-market-adx:mode": "below",
                                     "cond:o_ic-market-adx:value": 20.0}))["o_ic-market-adx"]
    assert (below["op"], below["comparison"], below["mode"], below["value"]) == (
        "<", "<", "below", 20.0)
    s2 = _strategy(_numeric_leaf())
    above = _by_id(decode_params(s2, {"cond:o_ic-market-adx:mode": "above",
                                      "cond:o_ic-market-adx:value": 30.0}))["o_ic-market-adx"]
    assert (above["op"], above["comparison"], above["mode"], above["value"]) == (
        ">", ">", "above", 30.0)
    # the template is never mutated
    assert s2.entry_rules[0]["conditions"]["conditions"][1]["op"] == "<"
    assert "mode" not in s2.entry_rules[0]["conditions"]["conditions"][1]


def test_decode_categorical_choice_sets_equality_and_the_code_as_value(state_field):
    s = _strategy(_cat_leaf())
    leaf = _by_id(decode_params(s, {"cond:o_ic-market-state:mode": "bear"}))["o_ic-market-state"]
    assert (leaf["op"], leaf["comparison"], leaf["mode"]) == ("==", "==", "bear")
    assert leaf["value"] == 2.0 and isinstance(leaf["value"], float)


def test_decode_categorical_choice_not_in_the_leaf_choices_raises(state_field):
    s = _strategy(_cat_leaf())
    with pytest.raises(ValueError, match="o_ic-market-state"):
        decode_params(s, {"cond:o_ic-market-state:mode": "sideways"})


def test_decode_accepts_the_choice_as_index_or_token(state_field):
    s = _strategy(_numeric_leaf(), _cat_leaf())
    by_token = decode_params(s, {"cond:o_ic-market-adx:mode": "above",
                                 "cond:o_ic-market-adx:value": 30.0,
                                 "cond:o_ic-market-state:mode": "chop"})
    by_index = decode_params(s, {"cond:o_ic-market-adx:mode": 2,
                                 "cond:o_ic-market-adx:value": 30.0,
                                 "cond:o_ic-market-state:mode": 3})
    assert by_token == by_index
    assert _by_id(by_index)["o_ic-market-state"]["mode"] == "chop"
    off = decode_params(s, {"cond:o_ic-market-adx:mode": 0, "cond:o_ic-market-state:mode": 0})
    assert [lf["id"] for lf in _leaves(off)] == ["base"]
    with pytest.raises(ValueError, match="o_ic-market-adx"):
        decode_params(s, {"cond:o_ic-market-adx:mode": 3})


def test_decode_a_mode_gene_on_a_leaf_without_mode_choices_raises():
    s = _strategy()  # 'base' carries no mode metadata
    with pytest.raises(ValueError, match="base"):
        decode_params(s, {"cond:base:mode": "off"})


# The pinned literal below was produced by the UNMODIFIED decode (HEAD 7cde782f) on this
# fixture: a value gene, a value_offset_from width gene, a toggle-dropped leaf and a
# confirmation_bars gene. A tree without mode leaves must decode byte-identically.
_LEGACY_RULES = [{"id": "r1", "continue_processing": False,
                  "actions": [{"action_type": "buy"}],
                  "conditions": {"operator": "AND", "conditions": [
                      {"id": "lo", "field": "price_vs_target", "comparison": ">", "op": ">",
                       "value": 0.0, "optimize": True, "value_min": -5.0, "value_max": 5.0,
                       "value_step": 1.0},
                      {"id": "hi", "field": "price_vs_target", "comparison": "<", "value": 4.0,
                       "value_offset_from": "lo", "optimize": True, "value_min": 1.0,
                       "value_max": 6.0, "value_step": 1.0, "toggle_optimize": True},
                      {"id": "gone", "field": "rsi_14", "comparison": "<", "value": 30.0,
                       "toggle_optimize": True},
                      {"id": "cb", "field": "rsi_14", "comparison": ">=", "value": 50.0,
                       "confirmation_bars_min": 1, "confirmation_bars_max": 3,
                       "confirmation_bars_step": 1}]}}]
_LEGACY_PIN = (
    '[{"actions": [{"action_type": "buy"}], "conditions": {"conditions": [{"comparison": ">", '
    '"field": "price_vs_target", "id": "lo", "op": ">", "optimize": true, "value": 2.0, '
    '"value_max": 5.0, "value_min": -5.0, "value_step": 1.0}, {"comparison": "<", "field": '
    '"price_vs_target", "id": "hi", "optimize": true, "toggle_optimize": true, "value": 5.0, '
    '"value_max": 6.0, "value_min": 1.0, "value_offset_from": "lo", "value_step": 1.0}, '
    '{"comparison": ">=", "confirmation_bars": 2, "confirmation_bars_max": 3, '
    '"confirmation_bars_min": 1, "confirmation_bars_step": 1, "field": "rsi_14", "id": "cb", '
    '"value": 50.0}], "operator": "AND"}, "continue_processing": false, "id": "r1"}]'
)


def test_decode_leaves_a_legacy_leaf_without_mode_unchanged():
    s = types.SimpleNamespace(entry_rules=copy.deepcopy(_LEGACY_RULES), exit_rules=[])
    out = decode_params(s, {"cond:lo:value": 2.0, "cond:hi:value": 3.0, "cond:hi:enabled": 1,
                            "cond:gone:enabled": 0, "cond:cb:confirmation_bars": 2})
    assert json.dumps(out["entry_rules"], sort_keys=True) == _LEGACY_PIN


def test_all_off_control_decodes_every_new_leaf_out(state_field):
    leaves = [_numeric_leaf(id=f"o_ic-market-{s}") for s in ("slope", "adx", "rv")]
    s = _strategy(*leaves, _cat_leaf())
    flat = {f"cond:o_ic-market-{sh}:mode": "off" for sh in ("slope", "adx", "rv", "state")}
    flat.update({f"cond:o_ic-market-{sh}:value": 20.0 for sh in ("slope", "adx", "rv")})
    out = decode_params(s, flat)
    template = _strategy(with_base=True)  # the same rule without any market leaf
    assert out["entry_rules"] == template.entry_rules


def test_encode_decode_roundtrip_keeps_mode_index_and_value(state_field):
    s = _strategy(_numeric_leaf(), _cat_leaf())
    space = collect_param_space(s)
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)
    flat_in = {"cond:o_ic-market-adx:value": 30.0, "cond:o_ic-market-adx:mode": "above",
               "cond:o_ic-market-state:mode": "bear"}
    ind = opt.encode_params(flat_in)
    names = list(space)
    assert ind[names.index("cond:o_ic-market-adx:mode")] == 2
    assert ind[names.index("cond:o_ic-market-state:mode")] == 2
    flat = opt.decode_individual(ind)
    assert flat == flat_in
    leaves = _by_id(decode_params(s, flat))
    assert (leaves["o_ic-market-adx"]["comparison"], leaves["o_ic-market-adx"]["value"]) == (
        ">", 30.0)
    assert (leaves["o_ic-market-state"]["comparison"], leaves["o_ic-market-state"]["value"]) == (
        "==", 2.0)


def test_anchor_canonicalisation_or_where_dedup_lives():
    """Phenotype dedup is NOT in this module: the trial memo key is built in
    ``strategy_optimization_handler`` (``trial_key({... "params": decoded_flat})`` -- the RAW
    decoded genome), so the inactive-threshold anchor canonicalisation belongs there (Task 8).
    This module must not grow a half-implemented canonicaliser, and decode must not rewrite
    the raw genome it was handed."""
    assert not [n for n in dir(sps)
                if any(w in n.lower() for w in ("canonical", "phenotype", "dedup"))]
    s = _strategy(_numeric_leaf())
    flat = {"cond:o_ic-market-adx:mode": "off", "cond:o_ic-market-adx:value": 35.0}
    before = dict(flat)
    decode_params(s, flat)
    assert flat == before
