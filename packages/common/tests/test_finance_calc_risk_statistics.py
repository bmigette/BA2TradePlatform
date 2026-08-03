import pytest


def test_pct_returns_and_align():
    from ba2_common.core.finance_calc.risk import pct_returns, align
    assert pct_returns([100.0, 102.0, 101.0]) == pytest.approx([0.02, -0.0098039], rel=1e-5)
    a, b = align([1, 2, 3, 4], [9, 8, 7])
    assert a == [2, 3, 4] and b == [9, 8, 7]  # most-recent common window


def test_beta_of_exact_2x_series():
    from ba2_common.core.finance_calc.risk import compute_beta
    # benchmark returns: +1%, -1%, +2%; asset returns exactly 2x: +2%, -2%, +4%
    res = compute_beta([100.0, 102.0, 99.96, 103.9584],
                       [100.0, 101.0, 99.99, 101.9898])
    assert res["beta"] == pytest.approx(2.0, abs=1e-3)
    assert res["correlation"] == pytest.approx(1.0, abs=1e-3)
    assert res["r_squared"] == pytest.approx(1.0, abs=1e-3)
    assert res["n_obs"] == 3


def test_correlation_matrix_perfect_pos_neg():
    from ba2_common.core.finance_calc.risk import compute_correlation
    # A returns +1%,+1%; B identical; C exactly -1x A
    res = compute_correlation({
        "A": [100.0, 101.0, 102.01],
        "B": [50.0, 50.5, 51.005],
        "C": [200.0, 198.0, 196.02],
    })
    assert res["matrix"]["A"]["B"] == pytest.approx(1.0, abs=1e-3)
    assert res["matrix"]["A"]["C"] == pytest.approx(-1.0, abs=1e-3)
    assert res["n_obs"] == 2


def test_var_historical_and_parametric():
    from ba2_common.core.finance_calc.risk import compute_var
    res = compute_var([100.0, 102.0, 101.0, 105.0, 103.0], 0.95, 1)
    assert res["n_obs"] == 4
    # historical: -percentile(sorted returns, 0.05) ~ 1.766%
    assert res["historical_var_pct"] == pytest.approx(0.017661, rel=1e-3)
    # parametric (normal): ~3.081%
    assert res["parametric_var_pct"] == pytest.approx(0.030807, rel=1e-3)


def test_descriptive_known_moments():
    from ba2_common.core.finance_calc.statistics import describe
    r = describe([1.0, 2.0, 3.0, 4.0, 5.0])
    assert r["mean"] == 3.0
    assert r["std_sample"] == pytest.approx(1.58113883, rel=1e-6)
    assert r["std_population"] == pytest.approx(1.41421356, rel=1e-6)
    assert r["median"] == 3.0 and r["p25"] == 2.0 and r["p75"] == 4.0 and r["iqr"] == 2.0
    assert r["skewness"] == pytest.approx(0.0, abs=1e-9)
    assert r["excess_kurtosis"] == pytest.approx(-1.912, abs=1e-3)


def test_regression_known_fit():
    from ba2_common.core.finance_calc.statistics import ols
    r = ols([1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 4.0, 5.0, 4.0, 5.0])
    assert r["slope"] == pytest.approx(0.6, rel=1e-9)
    assert r["intercept"] == pytest.approx(2.2, rel=1e-9)
    assert r["r_squared"] == pytest.approx(0.6, rel=1e-6)
    assert r["t_slope"] == pytest.approx(2.1213, rel=1e-3)
    assert 0.10 < r["p_value_slope"] < 0.15  # df=3, two-sided


def test_regression_constant_x_raises():
    from ba2_common.core.finance_calc.statistics import ols
    with pytest.raises(ValueError):
        ols([1.0, 1.0, 1.0], [2.0, 3.0, 4.0])


def test_renderers_produce_markdown():
    from ba2_common.core.finance_calc.risk import BetaRequest, render_beta, VarRequest, render_var
    from ba2_common.core.finance_calc.statistics import DescriptiveRequest, render_descriptive
    assert "**Beta**" in render_beta(BetaRequest(asset_prices=[100, 102, 99.96, 103.9584],
                                                 benchmark_prices=[100, 101, 99.99, 101.9898]))
    assert "Value-at-Risk" in render_var(VarRequest(prices=[100, 102, 101, 105, 103]))
    assert "Descriptive statistics" in render_descriptive(DescriptiveRequest(data=[1.0, 2.0, 3.0]))
