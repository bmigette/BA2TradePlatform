from ba2_trade_platform.modules.experts.FactorRanker.factors import (
    cross_sectional_zscore,
    composite_score,
    rank_symbols,
)


def test_zscore_centers_and_scales():
    z = cross_sectional_zscore({"A": 1.0, "B": 2.0, "C": 3.0})
    assert round(z["B"], 6) == 0.0          # B is the mean
    assert z["A"] < 0 < z["C"]


def test_composite_weights_and_zero_weight_disables():
    factors = {
        "momentum": {"A": 3.0, "B": 1.0},
        "value":    {"A": 1.0, "B": 3.0},
    }
    comp = composite_score(factors, weights={"momentum": 1.0, "value": 0.0})
    # value disabled -> A (high momentum) ranks above B
    assert comp["A"] > comp["B"]


def test_rank_descending():
    assert rank_symbols({"A": 0.1, "B": 0.9, "C": 0.5}) == ["B", "C", "A"]
