"""Template leaves for the SPY benchmark gates (plan 2026-10-24-spy-regime-gate, Task 5)."""
from ba2_common.core import benchmark_regime as br
from ba2_common.core.market_condition_templates import benchmark_regime_leaves, market_condition_leaves
from ba2_common.core.rule_models import ConditionLeaf


def test_ids_fields_and_order():
    leaves = benchmark_regime_leaves("o_csp")
    assert [l["id"] for l in leaves] == ["o_csp-spy-trend", "o_csp-spy-vol"]
    assert [l["field"] for l in leaves] == [br.TREND_FIELD, br.VOL_FIELD]


def test_mode_choices_follow_the_persisted_code_order():
    trend, vol = benchmark_regime_leaves("x")
    assert trend["mode_choices"] == ["off", *br.TREND_MODE_CODES]
    assert vol["mode_choices"] == ["off", *br.VOL_MODE_CODES]


def test_shape_is_the_one_market_condition_leaves_emits_for_a_categorical_field():
    (ref,) = [l for l in market_condition_leaves("x", ["ta-structure-v1"])
              if l["field"] == "structure_state"]
    for leaf in benchmark_regime_leaves("x"):
        assert set(leaf) == set(ref)
        assert {k: v for k, v in leaf.items() if k not in ("id", "field", "mode_choices")} == \
               {k: v for k, v in ref.items() if k not in ("id", "field", "mode_choices")}
        for absent in ("optimize", "value", "toggle_optimize"):
            assert absent not in leaf


def test_leaves_validate_as_condition_leaves():
    for leaf in benchmark_regime_leaves("x"):
        model = ConditionLeaf(**leaf)
        assert model.field == leaf["field"] and model.mode_optimize is True


def test_pinned_benchmark_leaf_is_resolved_gene_free_and_live_acceptable():
    import pytest
    from ba2_common.core.market_condition_rules import _unresolved_reason
    from ba2_common.core.market_condition_templates import pinned_benchmark_leaf
    from ba2_common.core.rule_models import normalize_tree

    leaf = pinned_benchmark_leaf("x-spy-trend", "spy_trend_regime", "below_200")
    assert leaf["op"] == "==" and leaf["mode"] == "below_200" and leaf["value"] == 6.0
    assert not leaf.get("mode_optimize") and not leaf.get("mode_choices")
    assert _unresolved_reason(leaf) == ""
    kept = normalize_tree(dict(leaf))
    assert kept["value"] == 6.0 and kept["field"] == "spy_trend_regime"
    with pytest.raises(ValueError):
        pinned_benchmark_leaf("x", "spy_trend_regime", "sideways")
    with pytest.raises(ValueError):
        pinned_benchmark_leaf("x", "adx", "up")
