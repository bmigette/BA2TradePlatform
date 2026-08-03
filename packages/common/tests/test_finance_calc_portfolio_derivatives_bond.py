import pytest


def test_performance_known_values():
    from ba2_common.core.finance_calc.portfolio import performance
    r = performance([0.02, -0.01, 0.03], periods_per_year=12, risk_free_annual=0.0)
    assert r["annualized_return"] == pytest.approx(0.170282, rel=1e-3)
    assert r["annualized_volatility"] == pytest.approx(0.072125, rel=1e-3)
    assert r["sharpe"] == pytest.approx(2.21886, rel=1e-3)
    assert r["max_drawdown"] == pytest.approx(-0.01, rel=1e-9)
    assert r["calmar"] == pytest.approx(17.0282, rel=1e-3)
    assert r["t_stat"] == pytest.approx(1.10943, rel=1e-3)  # sharpe * sqrt(3/12)


def test_performance_with_benchmark():
    from ba2_common.core.finance_calc.portfolio import performance
    r = performance([0.02, -0.02, 0.04], periods_per_year=12,
                    benchmark=[0.01, -0.01, 0.02])
    # asset is exactly 2x the benchmark -> beta 2
    assert r["beta"] == pytest.approx(2.0, rel=1e-3)
    assert "information_ratio" in r and "up_capture" in r


def test_black_scholes_textbook_call_put():
    from ba2_common.core.finance_calc.derivatives import black_scholes
    call = black_scholes(100.0, 100.0, 1.0, 0.05, 0.2, option_type="call")
    assert call["price"] == pytest.approx(10.4506, abs=1e-3)
    assert call["delta"] == pytest.approx(0.6368, abs=1e-3)
    put = black_scholes(100.0, 100.0, 1.0, 0.05, 0.2, option_type="put")
    assert put["price"] == pytest.approx(5.5735, abs=1e-3)
    # put-call parity: C - P = S - K*e^-rT = 100 - 100*e^-0.05
    assert call["price"] - put["price"] == pytest.approx(100 - 100 * 0.9512294, rel=1e-3)


def test_bond_price_from_ytm_and_roundtrip():
    from ba2_common.core.finance_calc.fixed_income import bond_analytics
    r = bond_analytics(0.05, 10.0, frequency=2, face=100.0, ytm=0.04)
    assert r["price"] == pytest.approx(108.1757, rel=1e-3)
    assert r["premium_discount"] == "premium"
    assert r["macaulay_duration"] == pytest.approx(8.11, abs=0.05)
    # Round-trip: solve YTM from that price
    r2 = bond_analytics(0.05, 10.0, frequency=2, face=100.0, price=r["price"])
    assert r2["ytm"] == pytest.approx(0.04, rel=1e-3)


def test_bond_requires_exactly_one_of_ytm_price():
    from ba2_common.core.finance_calc.fixed_income import BondRequest
    with pytest.raises(ValueError):
        BondRequest(coupon_rate=0.05, years_to_maturity=10.0)  # neither
    with pytest.raises(ValueError):
        BondRequest(coupon_rate=0.05, years_to_maturity=10.0, ytm=0.04, price=108.0)  # both


def test_renderers_produce_markdown():
    from ba2_common.core.finance_calc.portfolio import PerformanceRequest, render_performance
    from ba2_common.core.finance_calc.derivatives import BlackScholesRequest, render_black_scholes
    from ba2_common.core.finance_calc.fixed_income import BondRequest, render_bond
    assert "Portfolio performance" in render_performance(
        PerformanceRequest(returns=[0.02, -0.01, 0.03]))
    assert "Black-Scholes call" in render_black_scholes(
        BlackScholesRequest(spot=100.0, strike=100.0, years=1.0, rate=0.05, vol=0.2))
    assert "**Bond**" in render_bond(BondRequest(coupon_rate=0.05, years_to_maturity=10.0, ytm=0.04))
