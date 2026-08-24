"""The RAW inputs behind every DeterministicScorer number must survive.

"Quality (ROE) -0.91" is unreadable on its own: what ROE, measured against
what, transformed how? Today the calculators throw the raw measurement away
and return only the normalized output -- `_build_fundamental` computes
`roe = net_income / equity` and keeps ONLY `tanh((roe - 0.10) / 0.10)`;
`piotroski_f_score` returns a bare int and discards which of the nine tests
passed; `growth_acceleration` returns `latest - trailing` and discards both.

These tests pin the evidence-carrying variants. Each one must report the raw
measured value, the comparator it was measured against, and the transformation
applied -- and must say so honestly when an input was never recorded rather
than back-solving a plausible number out of the output.
"""
import math

import pytest

from ba2_experts.DeterministicScorer.fundamental import (
    DEF_QUALITY_K, DEF_QUALITY_NEUTRAL_ROE, DEF_VALUE_K, DEF_VALUE_NEUTRAL_YIELD,
    altman_z_and_variant, altman_z_detail, fundamental_score,
    growth_acceleration, growth_acceleration_detail,
    piotroski_f_score, piotroski_f_score_detail,
)


# --------------------------------------------------------------------------
# Piotroski: which of the nine tests failed?
# --------------------------------------------------------------------------

_CUR = {
    "netIncome": 100.0, "totalAssets": 1000.0, "operatingCashFlow": 150.0,
    "longTermDebt": 200.0, "totalCurrentAssets": 400.0, "totalCurrentLiabilities": 200.0,
    "weightedAverageShsOut": 100.0, "grossProfit": 400.0, "revenue": 900.0,
}
_PRIOR = {
    "netIncome": 80.0, "totalAssets": 900.0, "longTermDebt": 150.0,
    "totalCurrentAssets": 350.0, "totalCurrentLiabilities": 200.0,
    "weightedAverageShsOut": 100.0, "grossProfit": 350.0, "revenue": 800.0,
}


def test_the_detail_score_equals_the_plain_score():
    """Refactor guard: the evidence variant must not change the number."""
    assert piotroski_f_score_detail(_CUR, _PRIOR)["score"] == piotroski_f_score(_CUR, _PRIOR)


@pytest.mark.parametrize("cur,prior", [
    ({}, {}), (_CUR, {}), ({}, _PRIOR),
    ({"netIncome": 1.0, "totalAssets": 2.0}, {"netIncome": 1.0, "totalAssets": 2.0}),
])
def test_the_detail_and_plain_scores_agree_on_the_none_cases_too(cur, prior):
    assert piotroski_f_score_detail(cur, prior)["score"] == piotroski_f_score(cur, prior)


def test_every_computed_test_is_reported_with_its_own_numbers():
    d = piotroski_f_score_detail(_CUR, _PRIOR)
    roa = next(c for c in d["components"] if c["name"] == "roa_positive")
    assert roa["passed"] is True
    assert roa["current"] == pytest.approx(0.10)      # 100/1000
    assert roa["comparator"] == 0.0


def test_a_failed_test_reports_the_two_values_that_made_it_fail():
    """Long-term debt/assets went UP (0.20 vs 0.1667), so the leverage point is
    lost -- and the user can see both ratios, not just the missing point."""
    d = piotroski_f_score_detail(_CUR, _PRIOR)
    lev = next(c for c in d["components"] if c["name"] == "leverage_decreased")
    assert lev["passed"] is False
    assert lev["current"] == pytest.approx(0.2)
    assert lev["comparator"] == pytest.approx(150.0 / 900.0)


def test_an_uncomputable_test_is_reported_as_uncomputable_not_as_a_failure():
    """A missing input is NOT a failed test: Piotroski does not count it
    against the score, and the detail must not imply that it did."""
    cur = dict(_CUR); cur.pop("grossProfit")
    d = piotroski_f_score_detail(cur, _PRIOR)
    gm = next(c for c in d["components"] if c["name"] == "gross_margin_increased")
    assert gm["passed"] is None
    assert gm["current"] is None
    assert d["computed"] == 8


def test_the_reported_components_add_up_to_the_reported_score():
    d = piotroski_f_score_detail(_CUR, _PRIOR)
    assert sum(1 for c in d["components"] if c["passed"]) == d["score"]


def test_too_few_computable_tests_yields_a_none_score_but_still_shows_what_was_measured():
    d = piotroski_f_score_detail({"netIncome": 1.0, "totalAssets": 2.0},
                                 {"netIncome": 1.0, "totalAssets": 2.0})
    assert d["score"] is None
    assert d["computed"] < 6
    assert any(c["passed"] is not None for c in d["components"])


def test_no_statements_at_all_reports_nothing_measured():
    d = piotroski_f_score_detail({}, {})
    assert d["score"] is None
    assert d["computed"] == 0


# --------------------------------------------------------------------------
# Altman Z: the terms, and the cutoff it was compared against
# --------------------------------------------------------------------------

_BAL = {
    "totalAssets": 1000.0, "totalCurrentAssets": 400.0, "totalCurrentLiabilities": 200.0,
    "retainedEarnings": 300.0, "ebit": 120.0, "revenue": 900.0, "totalLiabilities": 500.0,
    "totalStockholdersEquity": 500.0,
}


def test_the_altman_detail_matches_the_plain_calculator():
    d = altman_z_detail(_BAL, market_cap=2000.0)
    z, variant = altman_z_and_variant(_BAL, 2000.0)
    assert d["z"] == pytest.approx(z)
    assert d["variant"] == variant


def test_the_altman_detail_shows_the_weighted_terms_that_sum_to_z():
    d = altman_z_detail(_BAL, market_cap=2000.0)
    assert sum(t["contribution"] for t in d["terms"]) == pytest.approx(d["z"])


def test_each_altman_term_carries_its_own_ratio_and_coefficient():
    d = altman_z_detail(_BAL, market_cap=2000.0)
    x4 = next(t for t in d["terms"] if t["name"] == "X4")
    assert x4["ratio"] == pytest.approx(2000.0 / 500.0)
    assert x4["coefficient"] == pytest.approx(0.6)
    assert x4["contribution"] == pytest.approx(0.6 * 4.0)


def test_the_adjusted_variant_reports_its_own_coefficients():
    d = altman_z_detail(_BAL, market_cap=None)
    assert d["variant"] == "adjusted"
    assert {t["name"] for t in d["terms"]} == {"X1", "X2", "X3", "X4"}
    assert sum(t["contribution"] for t in d["terms"]) == pytest.approx(d["z"])


def test_an_uncomputable_altman_reports_none_rather_than_a_partial_sum():
    d = altman_z_detail({"totalAssets": 0.0}, market_cap=1.0)
    assert d["z"] is None
    assert d["variant"] is None


# --------------------------------------------------------------------------
# Growth acceleration: latest growth vs the trailing average it beat
# --------------------------------------------------------------------------

_REV = [100.0, 110.0, 121.0, 133.0, 160.0, 200.0]


def test_the_growth_detail_matches_the_plain_calculator():
    d = growth_acceleration_detail(_REV, min_points=3)
    assert d["acceleration"] == pytest.approx(growth_acceleration(_REV, min_points=3))


def test_the_growth_detail_exposes_both_sides_of_the_subtraction():
    d = growth_acceleration_detail(_REV, min_points=3)
    assert d["latest_growth"] == pytest.approx(200.0 / 160.0 - 1.0)
    assert d["acceleration"] == pytest.approx(d["latest_growth"] - d["trailing_mean"])
    assert d["n_trailing"] == 4


def test_the_growth_detail_reports_the_period_values_it_used():
    d = growth_acceleration_detail(_REV, min_points=3)
    assert d["latest_value"] == 200.0
    assert d["prior_value"] == 160.0
    assert d["n_values"] == 6


def test_too_short_a_history_reports_no_acceleration_and_says_how_short():
    d = growth_acceleration_detail([100.0, 110.0], min_points=3)
    assert d["acceleration"] is None
    assert d["n_values"] == 2
    assert d["min_points"] == 3


def test_the_trailing_window_is_capped_at_four_periods_as_the_calculator_does():
    long_hist = [float(v) for v in (10, 11, 12, 13, 14, 15, 16, 17, 30)]
    d = growth_acceleration_detail(long_hist, min_points=3)
    assert d["n_trailing"] == 4
    assert d["acceleration"] == pytest.approx(growth_acceleration(long_hist, min_points=3))


# --------------------------------------------------------------------------
# The quality/value transformation constants must be NAMED, not re-typed
# --------------------------------------------------------------------------

def test_the_quality_and_value_tanh_constants_are_module_level():
    """The detail text has to quote the SAME neutral point and scale the
    transformation used. Re-typing 0.10 in the explainer is how a displayed
    comparator silently stops matching the maths."""
    assert DEF_QUALITY_NEUTRAL_ROE == pytest.approx(0.10)
    assert DEF_QUALITY_K == pytest.approx(0.10)
    assert DEF_VALUE_NEUTRAL_YIELD == pytest.approx(0.10)
    assert DEF_VALUE_K == pytest.approx(0.10)


# --------------------------------------------------------------------------
# The section score's own composition
# --------------------------------------------------------------------------

def test_fundamental_score_components_reproduce_the_section_score():
    """A section detail that does not sum to the section score is worse than
    none. This is the arithmetic the card must be able to display."""
    snap = {"fscore": 4, "quality_norm": -0.91, "value_norm": -0.79,
            "rev_accel": 0.81, "eps_accel": 1.17}
    out = fundamental_score(snap, {})
    total_w = sum(c["weight"] for c in out["components"].values())
    recomputed = sum(c["weight"] * c["normalized"]
                     for c in out["components"].values()) / total_w
    assert recomputed == pytest.approx(out["score"])
    assert out["score"] == pytest.approx(-0.298, abs=5e-3)


def test_fundamental_score_reports_the_growth_input_it_actually_tanh_ed():
    """The 'growth' component is tanh(mean(rev_accel, eps_accel) / scale), and
    that mean is nowhere else in the output -- without it the row cannot be
    explained from the two acceleration rows the card shows."""
    snap = {"rev_accel": 0.81, "eps_accel": 1.17}
    out = fundamental_score(snap, {})
    g = out["components"]["growth"]
    assert g["raw"] == pytest.approx((0.81 + 1.17) / 2)
    assert g["scale"] == pytest.approx(0.05)
    assert g["normalized"] == pytest.approx(math.tanh(g["raw"] / g["scale"]))


def test_the_piotroski_component_reports_the_rescaling_it_applied():
    out = fundamental_score({"fscore": 4}, {})
    p = out["components"]["piotroski"]
    assert p["raw"] == 4
    assert p["normalized"] == pytest.approx((4 - 4.5) / 4.5)


def test_quality_and_value_components_carry_their_already_normalized_input():
    out = fundamental_score({"quality_norm": -0.91, "value_norm": -0.79}, {})
    assert out["components"]["quality"]["raw"] == pytest.approx(-0.91)
    assert out["components"]["value"]["raw"] == pytest.approx(-0.79)
