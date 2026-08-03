import pytest


def _dcf_req(**kw):
    from ba2_common.core.finance_calc.valuation import DCFRequest
    base = dict(fcf_schedule=[100.0, 110.0, 121.0], discount_rate=0.10,
                terminal_method="gordon_growth", terminal_growth_rate=0.02,
                net_debt=100.0, shares_outstanding=100.0)
    base.update(kw)
    return DCFRequest(**base)


def test_dcf_gordon_growth_known_values():
    from ba2_common.core.finance_calc.valuation import compute_dcf
    res = compute_dcf(_dcf_req())
    # Each year discounts to 100/1.1 = 90.9090... -> sum 272.7273
    assert res["sum_pv_explicit"] == pytest.approx(272.7272727, rel=1e-6)
    # TV = 121*1.02 / (0.10-0.02) = 1542.75; PV = 1542.75/1.331
    assert res["terminal_value"] == pytest.approx(1542.75, rel=1e-9)
    assert res["pv_terminal"] == pytest.approx(1159.0909091, rel=1e-6)
    assert res["enterprise_value"] == pytest.approx(1431.8181818, rel=1e-6)
    assert res["equity_value"] == pytest.approx(1331.8181818, rel=1e-6)
    assert res["intrinsic_per_share"] == pytest.approx(13.3181818, rel=1e-6)
    assert res["tv_share_of_ev"] == pytest.approx(0.8095238, rel=1e-4)


def test_dcf_exit_multiple():
    from ba2_common.core.finance_calc.valuation import compute_dcf
    res = compute_dcf(_dcf_req(terminal_method="exit_multiple", terminal_growth_rate=None,
                               terminal_ebitda=500.0, terminal_ebitda_multiple=8.0,
                               net_debt=0.0))
    assert res["terminal_value"] == pytest.approx(4000.0)
    assert res["enterprise_value"] == pytest.approx(3277.9864814, rel=1e-6)


def test_dcf_rejects_g_greater_equal_r():
    with pytest.raises(ValueError):
        _dcf_req(terminal_growth_rate=0.10)  # g == r is invalid


def test_render_dcf_mentions_terminal_share():
    from ba2_common.core.finance_calc.valuation import render_dcf
    out = render_dcf(_dcf_req())
    assert "Intrinsic value/share" in out
    assert "Terminal value is 81.0% of EV." in out


def test_wacc_capm_only():
    from ba2_common.core.finance_calc.valuation import CostOfCapitalRequest, compute_cost_of_capital
    res = compute_cost_of_capital(CostOfCapitalRequest(
        risk_free_rate=0.04, equity_risk_premium=0.05, beta=1.2))
    assert res["cost_of_equity"] == pytest.approx(0.10)
    assert res["wacc"] == pytest.approx(0.10)
    assert res["method"] == "CAPM only"


def test_wacc_blend():
    from ba2_common.core.finance_calc.valuation import CostOfCapitalRequest, compute_cost_of_capital
    res = compute_cost_of_capital(CostOfCapitalRequest(
        risk_free_rate=0.04, equity_risk_premium=0.05, beta=1.2,
        cost_of_debt=0.06, tax_rate=0.25, debt_to_equity=0.5))
    # E/V = 2/3, D/V = 1/3, after-tax Rd = 0.045 -> WACC = 0.0667 + 0.015
    assert res["wacc"] == pytest.approx(0.0816667, rel=1e-4)
    assert res["method"] == "CAPM + WACC"


def test_wacc_debt_trio_all_or_none():
    from ba2_common.core.finance_calc.valuation import CostOfCapitalRequest
    with pytest.raises(ValueError):
        CostOfCapitalRequest(risk_free_rate=0.04, equity_risk_premium=0.05, beta=1.2,
                             cost_of_debt=0.06)  # missing tax_rate + debt_to_equity


def test_sensitivity_grid_matches_point_dcf():
    from ba2_common.core.finance_calc.valuation import (
        DCFSensitivityRequest, compute_sensitivity, compute_dcf)
    req = DCFSensitivityRequest(
        fcf_schedule=[100.0, 110.0, 121.0], discount_rates=[0.09, 0.10],
        terminal_method="gordon_growth", terminal_growth_rates=[0.02, 0.03],
        net_debt=100.0, shares_outstanding=100.0)
    grid = compute_sensitivity(req)
    assert grid["metric"] == "intrinsic_per_share"
    assert grid["n_valid"] == 4
    # The (0.10, 0.02) cell must equal the point DCF from test_dcf_gordon_growth_known_values.
    assert grid["grid"][1][0] == pytest.approx(13.3181818, rel=1e-6)
    assert grid["low"] == min(v for row in grid["grid"] for v in row)
    assert grid["high"] == max(v for row in grid["grid"] for v in row)


def test_sensitivity_invalid_cell_is_none():
    from ba2_common.core.finance_calc.valuation import DCFSensitivityRequest, compute_sensitivity
    req = DCFSensitivityRequest(
        fcf_schedule=[100.0], discount_rates=[0.10],
        terminal_method="gordon_growth", terminal_growth_rates=[0.02, 0.10],
        net_debt=0.0, shares_outstanding=100.0)
    grid = compute_sensitivity(req)
    assert grid["grid"][0][1] is None  # g == r cell is blank, not fudged
    assert grid["n_valid"] == 1
