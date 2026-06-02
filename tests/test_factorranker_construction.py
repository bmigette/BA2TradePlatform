from ba2_trade_platform.modules.experts.FactorRanker.construction import long_only_top_n


def test_equal_weight_top_n_caps_and_sums_to_gross():
    ranked = ["A", "B", "C", "D"]
    scores = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.5}
    w = long_only_top_n(ranked, scores, top_n=2, weighting="equal",
                        max_weight_per_name=1.0, gross_exposure=1.0)
    assert set(w) == {"A", "B"}
    assert round(sum(w.values()), 6) == 1.0
    assert round(w["A"], 6) == round(w["B"], 6) == 0.5


def test_cap_applies():
    ranked = ["A", "B", "C"]
    scores = {"A": 3.0, "B": 2.0, "C": 1.0}
    w = long_only_top_n(ranked, scores, top_n=3, weighting="equal",
                        max_weight_per_name=0.25, gross_exposure=1.0)
    assert all(v <= 0.25 + 1e-9 for v in w.values())


def test_score_weighting_redistributes_freed_budget():
    # A dominates; with a 0.5 cap its excess is water-filled to B and C, so the
    # full gross is deployed and no single name breaches the cap.
    ranked = ["A", "B", "C"]
    scores = {"A": 10.0, "B": 1.0, "C": 1.0}
    w = long_only_top_n(ranked, scores, top_n=3, weighting="score",
                        max_weight_per_name=0.5, gross_exposure=1.0)
    assert round(w["A"], 6) == 0.5
    assert round(sum(w.values()), 6) == 1.0
    assert round(w["B"], 6) == round(w["C"], 6) == 0.25
