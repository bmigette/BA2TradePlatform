"""``repair_fr_top_n_below_pool`` -- the ``--fr-top-n-below-pool`` opt-in constraint (operator
decision 2026-09-29, the "ranking inert" trap: FactorRanker's ``long_only_top_n`` slices
``ranked[:top_n]``, which discards nothing when ``top_n >= screener_max_stocks``).
"""
from ba2_common.core.factor_ranker_topn import repair_fr_top_n_below_pool


def test_no_op_when_top_n_below_pool():
    """15/20: top_n already strictly below the pool -- nothing to repair."""
    settings = {"top_n": 15, "screener_max_stocks": 20, "weighting": "equal"}
    out, repaired = repair_fr_top_n_below_pool(settings)
    assert repaired is False
    assert out["top_n"] == 15
    assert out is settings  # unchanged input returned as-is, not a copy


def test_repairs_25_over_20_to_15():
    out, repaired = repair_fr_top_n_below_pool({"top_n": 25, "screener_max_stocks": 20})
    assert repaired is True
    assert out["top_n"] == 15


def test_repairs_equal_20_over_20_to_15():
    out, repaired = repair_fr_top_n_below_pool({"top_n": 20, "screener_max_stocks": 20})
    assert repaired is True
    assert out["top_n"] == 15


def test_repairs_40_over_10_to_the_floor_of_5():
    out, repaired = repair_fr_top_n_below_pool({"top_n": 40, "screener_max_stocks": 10})
    assert repaired is True
    assert out["top_n"] == 5


def test_input_dict_is_never_mutated():
    settings = {"top_n": 25, "screener_max_stocks": 20}
    out, repaired = repair_fr_top_n_below_pool(settings)
    assert repaired is True
    assert settings["top_n"] == 25  # original untouched
    assert out is not settings


def test_other_keys_survive_the_repair():
    settings = {"top_n": 25, "screener_max_stocks": 20, "weighting": "rank",
               "universe_source": "screener"}
    out, repaired = repair_fr_top_n_below_pool(settings)
    assert repaired is True
    assert out["weighting"] == "rank"
    assert out["universe_source"] == "screener"


def test_missing_top_n_is_a_no_op():
    settings = {"screener_max_stocks": 20}
    out, repaired = repair_fr_top_n_below_pool(settings)
    assert repaired is False
    assert out == settings


def test_missing_screener_max_stocks_is_a_no_op():
    """Static-universe (non-screener) FactorRanker settings carry top_n but never
    screener_max_stocks -- the repair must never fire for them."""
    settings = {"top_n": 25}
    out, repaired = repair_fr_top_n_below_pool(settings)
    assert repaired is False
    assert out == settings


def test_non_numeric_values_are_a_no_op():
    settings = {"top_n": "not-a-number", "screener_max_stocks": 20}
    out, repaired = repair_fr_top_n_below_pool(settings)
    assert repaired is False
    assert out == settings


def test_string_ints_are_coerced():
    """Genes/settings can round-trip through JSON as strings; the check must still fire."""
    out, repaired = repair_fr_top_n_below_pool({"top_n": "25", "screener_max_stocks": "20"})
    assert repaired is True
    assert out["top_n"] == 15
