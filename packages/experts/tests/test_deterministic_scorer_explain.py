"""What the DeterministicScorer card SAYS about each of its numbers.

Pure string/table builders over the evidence the calculators now record. The
contract each one is held to is the same:

  * the raw measured value, with its unit
  * what it was compared against, and that comparator's value
  * the transformation that produced the displayed number
  * for a section, the components and weights, so the section score is visibly
    the sum of its parts

and, crucially, an input that was NEVER RECORDED must be reported as such --
never back-solved out of the output into a plausible-looking number.
"""
import math

import pytest

from ba2_experts.DeterministicScorer import explain
from ba2_experts.DeterministicScorer.fundamental import (
    DEF_QUALITY_K, DEF_QUALITY_NEUTRAL_ROE, fundamental_score,
    growth_acceleration_detail,
)


def _t(pair):
    """(text, table) -> the text."""
    return pair[0]


def _tbl(pair):
    return dict(pair[1])


# --------------------------------------------------------------------------
# Quality (ROE)
# --------------------------------------------------------------------------

_QUALITY_EV = {
    "roe": -0.0528, "net_income": -2_640_000_000.0, "equity": 50_000_000_000.0,
    "neutral": DEF_QUALITY_NEUTRAL_ROE, "k": DEF_QUALITY_K,
    "normalized": math.tanh((-0.0528 - 0.10) / 0.10),
}


def test_quality_names_the_raw_roe_with_its_unit():
    text = _t(explain.explain_quality(_QUALITY_EV))
    assert "-5.28%" in text or "−5.28%" in text


def test_quality_names_the_two_statement_lines_the_roe_came_from():
    table = _tbl(explain.explain_quality(_QUALITY_EV))
    assert "Net income" in table
    assert "Shareholder equity" in table
    assert "-2,640,000,000" in table["Net income"]
    assert "50,000,000,000" in table["Shareholder equity"]


def test_quality_states_the_comparator_and_that_it_is_a_fixed_threshold():
    text = _t(explain.explain_quality(_QUALITY_EV))
    assert "10.00%" in text
    assert "fixed" in text.lower()
    # It is NOT a sector median or a cross-sectional mean -- saying so matters,
    # because the reader's default assumption is a peer comparison.
    assert "not a sector" in text.lower() or "not a peer" in text.lower()


def test_quality_states_the_transformation_including_its_scale():
    text = _t(explain.explain_quality(_QUALITY_EV))
    assert "tanh" in text
    assert "0.10" in text


def test_quality_arithmetic_in_the_text_reproduces_the_displayed_number():
    table = _tbl(explain.explain_quality(_QUALITY_EV))
    assert table["Quality score"].startswith("-0.91")


def test_quality_with_no_recorded_evidence_says_so_instead_of_inventing_an_roe():
    text = _t(explain.explain_quality(None))
    assert explain.NOT_RECORDED in text
    assert "%" not in text, "no fabricated ROE may appear"


def test_quality_with_a_recorded_normalized_but_missing_raw_inputs_is_honest():
    """The half-way case: an older analysis kept quality_norm but not the ROE."""
    text = _t(explain.explain_quality({"roe": None, "normalized": -0.91,
                                       "neutral": 0.10, "k": 0.10}))
    assert explain.NOT_RECORDED in text
    assert "-0.91" in text


# --------------------------------------------------------------------------
# Value (earnings yield)
# --------------------------------------------------------------------------

_VALUE_EV = {
    "earnings_yield": 0.0212, "operating_income": 4_240_000_000.0,
    "enterprise_value": 200_000_000_000.0, "market_cap": 180_000_000_000.0,
    "total_liabilities": 30_000_000_000.0, "cash": 10_000_000_000.0,
    "neutral": 0.10, "k": 0.10, "normalized": math.tanh((0.0212 - 0.10) / 0.10),
}


def test_value_shows_how_enterprise_value_was_built():
    table = _tbl(explain.explain_value(_VALUE_EV))
    assert "Enterprise value" in table
    assert "market cap" in table["Enterprise value"].lower()
    assert "+" in table["Enterprise value"] and "-" in table["Enterprise value"]


def test_value_names_the_yield_and_its_fixed_neutral_point():
    text = _t(explain.explain_value(_VALUE_EV))
    assert "2.12%" in text
    assert "10.00%" in text


def test_value_says_it_is_EBIT_over_EV_not_earnings_over_price():
    """The row is labelled 'Value (earnings yield)', which reads as E/P. It is
    an OPERATING earnings yield on enterprise value -- a different number."""
    text = _t(explain.explain_value(_VALUE_EV))
    assert "operating income" in text.lower()
    assert "enterprise value" in text.lower()


def test_value_explicitly_denies_the_reading_its_label_invites():
    """The row is labelled 'Value (earnings yield)'. Saying 'operating income'
    somewhere in the body is not enough -- the detail must actively rule out
    E/P, or a reader will keep the wrong mental model."""
    text = _t(explain.explain_value(_VALUE_EV)).lower()
    assert "not earnings-per-share over price" in text


def test_value_text_shows_the_enterprise_value_ARITHMETIC_not_just_the_total():
    """The comparator's derivation, not only its result: EV is built from three
    numbers and a reader cannot check it from the total alone."""
    text = _t(explain.explain_value(_VALUE_EV))
    assert "180,000,000,000" in text     # market cap
    assert "30,000,000,000" in text      # total liabilities
    assert "10,000,000,000" in text      # cash
    assert "200,000,000,000" in text     # the resulting EV


def test_value_with_no_recorded_evidence_says_so():
    assert explain.NOT_RECORDED in _t(explain.explain_value(None))


# --------------------------------------------------------------------------
# Growth acceleration
# --------------------------------------------------------------------------

def test_growth_shows_the_two_growth_rates_that_were_subtracted():
    d = growth_acceleration_detail([100.0, 110.0, 121.0, 133.0, 160.0, 200.0], min_points=3)
    table = _tbl(explain.explain_growth("Revenue", d))
    assert table["Latest period growth"].startswith("+25.00%")
    assert any(k.startswith("Trailing average growth") for k in table), table
    assert table["Acceleration"].startswith("+")


def test_growth_shows_the_period_values_behind_the_latest_growth():
    d = growth_acceleration_detail([100.0, 110.0, 121.0, 133.0, 160.0, 200.0], min_points=3)
    table = _tbl(explain.explain_growth("Revenue", d))
    assert "160" in table["Latest period"] and "200" in table["Latest period"]


def test_growth_states_how_many_trailing_periods_it_averaged():
    d = growth_acceleration_detail([100.0, 110.0, 121.0, 133.0, 160.0, 200.0], min_points=3)
    assert "4" in _t(explain.explain_growth("Revenue", d))


def test_growth_names_the_comparator_as_the_symbols_own_history():
    """It is a SELF-comparison over time. Calling it a sector median (the
    reader's default assumption for 'compared against') would be a flat lie
    about what was measured."""
    d = growth_acceleration_detail([100.0, 110.0, 121.0, 133.0, 160.0, 200.0], min_points=3)
    text = _t(explain.explain_growth("Revenue", d)).lower()
    assert "own trailing" in text
    assert "not a peer comparison" in text
    assert "sector median" not in text


def test_growth_with_no_recorded_detail_says_so():
    assert explain.NOT_RECORDED in _t(explain.explain_growth("Revenue", None))


# --------------------------------------------------------------------------
# Piotroski
# --------------------------------------------------------------------------

_CUR = {"netIncome": 100.0, "totalAssets": 1000.0, "operatingCashFlow": 150.0,
        "longTermDebt": 200.0, "totalCurrentAssets": 400.0,
        "totalCurrentLiabilities": 200.0, "weightedAverageShsOut": 100.0,
        "grossProfit": 400.0, "revenue": 900.0}
_PRIOR = {"netIncome": 80.0, "totalAssets": 900.0, "longTermDebt": 150.0,
          "totalCurrentAssets": 350.0, "totalCurrentLiabilities": 200.0,
          "weightedAverageShsOut": 100.0, "grossProfit": 350.0, "revenue": 800.0}


def test_piotroski_lists_every_test_with_pass_or_fail():
    from ba2_experts.DeterministicScorer.fundamental import piotroski_f_score_detail
    d = piotroski_f_score_detail(_CUR, _PRIOR)
    table = _tbl(explain.explain_piotroski(d))
    assert len(table) == 9
    assert any(v.startswith("PASS") for v in table.values())
    assert any(v.startswith("FAIL") for v in table.values())


def test_piotroski_shows_the_two_numbers_behind_a_failed_test():
    from ba2_experts.DeterministicScorer.fundamental import piotroski_f_score_detail
    d = piotroski_f_score_detail(_CUR, _PRIOR)
    table = _tbl(explain.explain_piotroski(d))
    lev = next(v for k, v in table.items() if "LT debt" in k)
    assert "0.2000" in lev and "0.1667" in lev


def test_piotroski_marks_an_uncomputable_test_as_such_not_as_a_failure():
    from ba2_experts.DeterministicScorer.fundamental import piotroski_f_score_detail
    cur = dict(_CUR); cur.pop("grossProfit")
    table = _tbl(explain.explain_piotroski(piotroski_f_score_detail(cur, _PRIOR)))
    gm = next(v for k, v in table.items() if "gross margin" in k.lower())
    assert gm.startswith("n/a")
    assert "FAIL" not in gm


def test_piotroski_with_no_recorded_detail_says_so():
    assert explain.NOT_RECORDED in _t(explain.explain_piotroski(None))


# --------------------------------------------------------------------------
# Altman Z
# --------------------------------------------------------------------------

_FUND_Z = {
    "z": 9.25, "z_variant": "original", "z_veto_used": 1.8, "veto": False,
    "evidence": {"altman": {
        "z": 9.25, "variant": "original",
        "terms": [{"name": "X4", "label": "market cap / total liabilities",
                   "ratio": 4.0, "coefficient": 0.6, "contribution": 2.4}]}},
}


def test_altman_states_the_cutoff_it_was_compared_against():
    text = _t(explain.explain_altman(_FUND_Z))
    assert "1.8" in text
    assert "original" in text


def test_altman_shows_the_weighted_terms():
    table = _tbl(explain.explain_altman(_FUND_Z))
    x4 = next(v for k, v in table.items() if k.startswith("X4"))
    assert "4.0000" in x4 and "0.6" in x4 and "2.4000" in x4


def test_altman_with_no_recorded_terms_still_reports_the_score_and_cutoff():
    text = _t(explain.explain_altman({"z": 9.25, "z_variant": "original",
                                      "z_veto_used": 1.8, "veto": False}))
    assert "9.25" in text and "1.8" in text
    assert explain.NOT_RECORDED in text


# --------------------------------------------------------------------------
# The FUNDAMENTAL section: -0.30 must visibly be the sum of its parts
# --------------------------------------------------------------------------

_SNAP = {"fscore": 4, "quality_norm": -0.91, "value_norm": -0.79,
         "rev_accel": 0.81, "eps_accel": 1.17}


def test_the_section_detail_lists_every_component_with_weight_and_contribution():
    fund = fundamental_score(_SNAP, {})
    table = _tbl(explain.explain_fundamental_section(fund))
    for name in ("piotroski", "quality", "value", "growth"):
        assert any(name in k for k in table), name


def test_the_component_contributions_sum_to_the_section_score():
    """A breakdown that does not add up is worse than no breakdown."""
    fund = fundamental_score(_SNAP, {})
    table = _tbl(explain.explain_fundamental_section(fund))
    contribs = [float(v.split("=")[-1].strip())
                for k, v in table.items() if k.startswith("Component")]
    assert sum(contribs) == pytest.approx(fund["score"], abs=1e-3)


def test_the_contributions_still_sum_to_the_score_when_a_LEG_IS_MISSING():
    """With all four legs present the weights already total 1.00, so
    `w x norm` and `w x norm / total_w` are indistinguishable -- a full-
    component fixture cannot detect a dropped renormalization. Drop a leg and
    the divisor stops being 1."""
    fund = fundamental_score({"fscore": 4, "quality_norm": -0.91}, {})
    table = _tbl(explain.explain_fundamental_section(fund))
    contribs = [float(v.split("=")[-1].strip())
                for k, v in table.items() if k.startswith("Component")]
    assert sum(contribs) == pytest.approx(fund["score"], abs=1e-3)
    assert float(table["Section score"]) == pytest.approx(fund["score"], abs=1e-3)


def test_the_reported_divisor_is_the_actual_sum_of_the_used_weights():
    """0.25 (piotroski) + 0.30 (quality) = 0.55, not 1.00."""
    fund = fundamental_score({"fscore": 4, "quality_norm": -0.91}, {})
    assert fund["weight_total"] == pytest.approx(0.55)
    assert "divisor = 0.55" in _t(explain.explain_fundamental_section(fund))


def test_the_section_detail_shows_the_weight_renormalization_explicitly():
    """Weights are renormalized over the components that WERE computable; a
    reader who cannot see the divisor cannot check the arithmetic."""
    fund = fundamental_score({"fscore": 4}, {})       # only one component
    text = _t(explain.explain_fundamental_section(fund))
    assert "0.25" in text            # the piotroski weight
    assert "renormal" in text.lower()


def test_the_section_detail_reports_which_components_were_missing():
    fund = fundamental_score({"fscore": 4}, {})
    text = _t(explain.explain_fundamental_section(fund))
    for missing in ("quality", "value", "growth"):
        assert missing in text


def test_a_section_score_of_none_is_explained_as_nothing_computable():
    fund = fundamental_score({}, {})
    text = _t(explain.explain_fundamental_section(fund))
    assert fund["score"] is None
    assert "no fundamental input" in text.lower()


def test_the_section_detail_flags_an_applied_veto():
    fund = fundamental_score({"fscore": 4, "z": 1.0, "z_variant": "original"}, {})
    assert fund["veto"] is True
    assert "veto" in _t(explain.explain_fundamental_section(fund)).lower()


# --------------------------------------------------------------------------
# TECHNICAL + MACRO sections
# --------------------------------------------------------------------------

_TECH = {
    "score": 0.58,
    "components": {
        "momentum_vol_adj": {"weight": 0.45, "raw": 0.9, "normalized": 0.537},
        "dist_sma_trend": {"weight": 0.25, "raw": 0.12, "normalized": 0.664},
        "rsi_meanrev": {"weight": 0.15, "raw": 61.0, "normalized": -0.583},
        "donchian_breakout": {"weight": 0.15, "raw": 1.0, "normalized": 1.0},
    },
    "adx": 31.0, "atr": 4.2, "trending": True, "n_signals": 4,
}


def test_the_technical_detail_shows_each_leg_raw_value_and_its_tanh_scale():
    table = _tbl(explain.explain_technical_section(_TECH, {}))
    mom = next(v for k, v in table.items() if "momentum" in k)
    assert "0.9" in mom          # the raw vol-adjusted momentum
    assert "1.5" in mom          # scale_momvol default, the k of the tanh


def test_the_technical_detail_reports_the_adx_gate_that_reweighted_the_legs():
    text = _t(explain.explain_technical_section(_TECH, {}))
    assert "ADX" in text and "31" in text
    assert "trending" in text.lower()


def test_the_technical_detail_reports_the_rsi_boost_when_not_trending():
    tech = dict(_TECH, adx=12.0, trending=False)
    text = _t(explain.explain_technical_section(tech, {}))
    assert "mean-reversion" in text.lower() or "rsi" in text.lower()


def test_a_leg_with_no_raw_value_is_reported_as_excluded_not_as_zero():
    tech = dict(_TECH, components=dict(
        _TECH["components"], donchian_breakout={"weight": 0.15, "raw": None,
                                                "normalized": 0.0}))
    table = _tbl(explain.explain_technical_section(tech, {}))
    don = next(v for k, v in table.items() if "donchian" in k)
    assert "excluded" in don.lower()


def test_the_macro_detail_lists_the_inputs_and_their_weights():
    regime = {"score": 0.78, "n_inputs": 5, "components": {
        "trend_index": {"weight": 0.30, "value": 1.0},
        "vix": {"weight": 0.25, "value": 0.8},
    }}
    table = _tbl(explain.explain_macro_section(regime))
    assert any("trend_index" in k for k in table)
    assert "0.30" in next(v for k, v in table.items() if "trend_index" in k)


def test_the_macro_detail_with_no_regime_says_macro_is_off():
    assert "off" in _t(explain.explain_macro_section(None)).lower()
