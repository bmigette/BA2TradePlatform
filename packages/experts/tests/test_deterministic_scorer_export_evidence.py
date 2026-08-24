"""End-to-end: the DeterministicScorer card explains its own numbers.

Runs the REAL _gather/_process through export_symbol_data against a fixed
statement fixture, then asserts that each rendered row carries the evidence
behind it -- the raw measurement, the comparator, the transformation -- and
that a section row's components visibly sum to the section score.

The fixture is deliberately a poor-fundamentals name (negative ROE, thin
earnings yield, 4/9 Piotroski) because that is the case the user asked about:
"why does this say fundamentals are not good?".
"""
import pytest

import pandas as pd

from ba2_experts.DeterministicScorer import DeterministicScorer
from ba2_experts.DeterministicScorer import data as _data

_CLOSES = [100.0 + i * 0.5 for i in range(280)]
_DF = pd.DataFrame({
    "Date": pd.date_range("2024-01-01", periods=280, freq="D"),
    "Open": _CLOSES, "High": [c + 1 for c in _CLOSES],
    "Low": [c - 1 for c in _CLOSES], "Close": _CLOSES,
    "Volume": [1_000_000] * 280,
})
_LAST_CLOSE = _CLOSES[-1]          # 239.5
_SHARES = 1_000_000_000.0

# Latest-first, as the provider returns them. 6 annual periods so the growth
# calculator has something to work with.
_REVENUE = [50_000_000_000.0, 44_000_000_000.0, 40_000_000_000.0,
            37_000_000_000.0, 35_000_000_000.0, 34_000_000_000.0]
_EPS = [-2.64, 1.20, 1.05, 0.95, 0.90, 0.88]

_INCOME = [{
    "total_revenue": rev,
    "gross_profit": rev * (0.40 if i else 0.38),   # margin FELL in the latest year
    "operating_income": rev * 0.10,
    "net_income": eps * _SHARES,
    "eps_diluted": eps,
    "weighted_average_shares_outstanding": _SHARES,
} for i, (rev, eps) in enumerate(zip(_REVENUE, _EPS))]

_BALANCE = [{
    "total_assets": 100_000_000_000.0 - i * 5_000_000_000.0,
    "total_current_assets": 40_000_000_000.0 - i * 1_000_000_000.0,
    "total_current_liabilities": 20_000_000_000.0,
    "retained_earnings": 30_000_000_000.0,
    "total_liabilities": 50_000_000_000.0,
    "total_shareholder_equity": 50_000_000_000.0,
    "cash_and_cash_equivalents": 10_000_000_000.0,
    "long_term_debt": 20_000_000_000.0 - i * 2_000_000_000.0,   # debt ROSE latest year
} for i in range(6)]

_CASHFLOW = [{"operating_cash_flow": 8_000_000_000.0} for _ in range(6)]


class _FakeOHLCV:
    def get_ohlcv_data(self, symbol=None, start_date=None, end_date=None, interval="1d"):
        return _DF


class _FakeDetails:
    def get_income_statement(self, **kw):
        return {"statements": _INCOME}

    def get_balance_sheet(self, **kw):
        return {"statements": _BALANCE}

    def get_cashflow_statement(self, **kw):
        return {"statements": _CASHFLOW}


def _resolver(cat, name, **kw):
    return {"ohlcv": _FakeOHLCV(), "fundamentals_details": _FakeDetails()}.get(cat)


@pytest.fixture()
def export():
    _data.reset_caches()
    result = DeterministicScorer.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is None, result.error
    return result


def _row(export, label):
    return next(m for m in export.metrics if m.label == label)


def _tbl(metric):
    return dict(metric.detail_table or [])


# --------------------------------------------------------------------------
# Quality (ROE)
# --------------------------------------------------------------------------

def test_quality_row_shows_the_actual_roe_it_measured(export):
    row = _row(export, "Quality (ROE)")
    # net income = -2.64 x 1e9 shares = -2.64e9; equity = 5.0e10 -> ROE = -5.28%
    assert "-5.28%" in row.detail
    assert _tbl(row)["ROE (raw)"] == "-5.28%"
    assert _tbl(row)["Net income"] == "-2,640,000,000"
    assert _tbl(row)["Shareholder equity"] == "50,000,000,000"


def test_quality_row_shows_the_comparator_and_the_transformation(export):
    row = _row(export, "Quality (ROE)")
    assert "10.00%" in row.detail
    assert "tanh" in row.detail
    assert "fixed" in row.detail.lower()


def test_quality_rows_arithmetic_reproduces_the_displayed_value(export):
    row = _row(export, "Quality (ROE)")
    assert row.display.startswith("-0.91")
    assert _tbl(row)["Quality score"].startswith("-0.91")


# --------------------------------------------------------------------------
# Value (earnings yield)
# --------------------------------------------------------------------------

def test_value_row_shows_the_enterprise_value_it_divided_by(export):
    row = _row(export, "Value (earnings yield)")
    table = _tbl(row)
    market_cap = _SHARES * _LAST_CLOSE
    ev = market_cap + 50_000_000_000.0 - 10_000_000_000.0
    assert table["Market cap"] == f"{market_cap:,.0f}"
    assert table["Enterprise value"].startswith(f"{ev:,.0f}")
    assert table["Operating income (EBIT)"] == f"{_REVENUE[0] * 0.10:,.0f}"


def test_value_row_says_which_yield_it_is(export):
    detail = _row(export, "Value (earnings yield)").detail.lower()
    assert "operating income" in detail
    assert "enterprise value" in detail


# --------------------------------------------------------------------------
# Growth
# --------------------------------------------------------------------------

def test_revenue_acceleration_row_shows_both_growth_rates(export):
    table = _tbl(_row(export, "Revenue acceleration"))
    # latest annual revenue 44.0bn -> 50.0bn = +13.6364%
    assert table["Latest period growth"].startswith("+13.64%")
    assert any(k.startswith("Trailing average growth") for k in table)


def test_eps_acceleration_row_shows_the_negative_base_handling(export):
    row = _row(export, "EPS acceleration")
    assert "|prior|" in row.detail
    assert _tbl(row)["Latest period"] == "1 → -3"    # 1.20 -> -2.64, rounded display


# --------------------------------------------------------------------------
# Piotroski
# --------------------------------------------------------------------------

def test_piotroski_row_lists_all_nine_tests_with_their_numbers(export):
    row = _row(export, "Piotroski F-Score")
    table = _tbl(row)
    assert len(table) == 9
    lev = next(v for k, v in table.items() if "LT debt" in k)
    assert lev.startswith("FAIL"), lev     # long-term debt rose


def test_piotroski_passes_and_fails_add_up_to_the_displayed_score(export):
    row = _row(export, "Piotroski F-Score")
    n_pass = sum(1 for v in _tbl(row).values() if v.startswith("PASS"))
    assert row.display == f"{n_pass} / 9"


# --------------------------------------------------------------------------
# Altman
# --------------------------------------------------------------------------

def test_altman_row_states_the_cutoff_for_the_variant_used(export):
    row = _row(export, "Altman Z")
    assert "1.8" in row.detail          # original-variant distress cutoff
    assert "original" in row.detail


def test_altman_terms_sum_to_the_displayed_z(export):
    row = _row(export, "Altman Z")
    table = _tbl(row)
    contribs = [float(v.split("=")[-1].strip().replace(",", ""))
                for k, v in table.items() if k.startswith("X")]
    assert sum(contribs) == pytest.approx(row.value, abs=1e-3)


# --------------------------------------------------------------------------
# The FUNDAMENTAL section itself
# --------------------------------------------------------------------------

def test_fundamental_section_components_sum_to_the_section_score(export):
    row = _row(export, "Fundamental section")
    contribs = [float(v.split("=")[-1].strip())
                for k, v in _tbl(row).items() if k.startswith("Component")]
    assert sum(contribs) == pytest.approx(row.value, abs=1e-3)


def test_fundamental_section_names_every_component_and_its_weight(export):
    table = _tbl(_row(export, "Fundamental section"))
    for name in ("piotroski", "quality", "value", "growth"):
        assert f"Component {name}" in table, name
    assert "0.30" in table["Component quality"]     # fw_quality default


def test_fundamental_section_explains_the_renormalization(export):
    assert "renormal" in _row(export, "Fundamental section").detail.lower()


# --------------------------------------------------------------------------
# TECHNICAL + MACRO
# --------------------------------------------------------------------------

def test_technical_section_shows_each_leg_with_its_raw_reading(export):
    table = _tbl(_row(export, "Technical section"))
    assert any("momentum_vol_adj" in k for k in table)
    assert any("rsi_meanrev" in k for k in table)
    assert "Section score" in table


def test_technical_section_contributions_sum_to_the_section_score(export):
    row = _row(export, "Technical section")
    contribs = [float(v.split("=")[-1].strip())
                for k, v in _tbl(row).items() if "weight" in v and "=" in v]
    assert sum(contribs) == pytest.approx(row.value, abs=1e-3)


def test_macro_row_carries_a_detail_even_when_only_the_index_trend_resolves(export):
    row = _row(export, "Macro regime")
    assert row.detail


# --------------------------------------------------------------------------
# Honesty: nothing invented when the inputs were not there
# --------------------------------------------------------------------------

def test_no_row_claims_a_raw_value_when_the_statements_were_empty():
    class _Empty:
        def get_income_statement(self, **kw):
            return {"statements": []}

        def get_balance_sheet(self, **kw):
            return {"statements": []}

        def get_cashflow_statement(self, **kw):
            return {"statements": []}

    _data.reset_caches()
    result = DeterministicScorer.export_symbol_data(
        "AAPL",
        providers_resolver=lambda cat, name, **kw: {
            "ohlcv": _FakeOHLCV(), "fundamentals_details": _Empty()}.get(cat))
    assert result.error is None, result.error
    labels = {m.label for m in result.metrics}
    assert "Quality (ROE)" not in labels
    fund = next(m for m in result.metrics if m.label == "Fundamental section")
    assert fund.value is None
    assert "no fundamental input" in fund.detail.lower()
