"""FactorRanker ``weighting="rank"``: the better the rank, the bigger the weight, capped.

Opt-in. ``equal`` stays the default, so an instance that never chose ``rank`` builds
exactly the book it built before.
"""
import pytest

from ba2_experts.FactorRanker import FactorRanker
from ba2_experts.FactorRanker.construction import long_only_top_n

RANKED = ["A", "B", "C", "D"]
SCORES = {"A": 2.0, "B": 1.0, "C": 0.5, "D": 0.1}


def test_rank_weights_are_linear_in_rank():
    w = long_only_top_n(RANKED, SCORES, top_n=4, weighting="rank")

    # 4 + 3 + 2 + 1 = 10 parts
    assert w == pytest.approx({"A": 0.4, "B": 0.3, "C": 0.2, "D": 0.1})


def test_rank_ignores_the_scores_spread():
    """Only the order matters -- a runaway top score does not swallow the book."""
    skewed = {"A": 100.0, "B": 1.0, "C": 0.5, "D": 0.1}

    assert (long_only_top_n(RANKED, skewed, top_n=4, weighting="rank")
            == long_only_top_n(RANKED, SCORES, top_n=4, weighting="rank"))


def test_the_per_name_cap_still_binds_and_the_excess_flows_down():
    w = long_only_top_n(RANKED, SCORES, top_n=4, weighting="rank", max_weight_per_name=0.3)

    assert w["A"] == pytest.approx(0.3)
    assert max(w.values()) <= 0.3 + 1e-12
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["B"] >= w["C"] >= w["D"]


def test_when_every_name_is_capped_the_rest_is_cash():
    w = long_only_top_n(RANKED, SCORES, top_n=4, weighting="rank", max_weight_per_name=0.2)

    assert w == pytest.approx({s: 0.2 for s in RANKED})


def test_equal_is_still_the_default_and_unchanged():
    assert FactorRanker.get_settings_definitions()["weighting"]["default"] == "equal"
    assert long_only_top_n(RANKED, SCORES, top_n=4) == pytest.approx({s: 0.25 for s in RANKED})


def test_the_setting_is_a_dropdown_offering_rank():
    d = FactorRanker.get_settings_definitions()["weighting"]

    assert d["valid_values"] == ["equal", "score", "rank"]
