"""An unmeasurable factor is NOT a factor of zero.

Every FactorRanker calculator used to answer 0.0 when it could not compute
anything: fewer than 252 closes, no ROE, no gross profit, no earnings estimate
dispersion. ``composite_score`` then z-scored that 0.0 across the universe and
averaged it in as though it had been measured -- and in a FALLING market a raw 0.0
momentum is the BEST reading in the cross-section, so the name with no data ranked
first and ``long_only_top_n`` bought it. No failure is needed to reach this: a
recent IPO has fewer than 252 closes.

The fix follows ``ba2_common.core.option_lifecycle``: a calculator that cannot
measure returns ``None``, never a number, and the composite renormalizes per symbol
over the factors that WERE measured, recording ``n_factors``. A name measured on
fewer than K factors is excluded from the ranking rather than scored on a fraction
of the model.

K = 2 (clamped to the number of weighted factors). A single measured z-score has
materially wider dispersion than a blend of several -- averaging shrinks -- so
admitting one-factor names systematically stuffs BOTH tails of the ranking with the
least-measured names, and a long-only top-N buys the top tail. That is the original
defect wearing a different hat. ``macro.DEF_MIN_INPUTS_FOR_RISKOFF = 2`` already
made the same call for the same reason ("one uncorroborated binary reading is not a
regime call"). K is clamped so a deliberately single-factor model still works.

The inverse error is pinned throughout: a factor genuinely measured AT zero -- a
flat 12-1 momentum, a zero earnings yield, a PEAD reading outside its drift window
-- is a measurement and must keep scoring 0.0.
"""
import logging
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_common.core.types import OrderRecommendation, Recommendation
from ba2_experts.FactorRanker import FactorRanker
from ba2_experts.FactorRanker.construction import long_only_top_n
from ba2_experts.FactorRanker.factors import (
    DEF_MIN_MEASURED_FACTORS, composite_detail, composite_score,
    cross_sectional_stats, cross_sectional_zscore, earnings_surprise,
    momentum_12_1, quality_score, rank_symbols, value_score,
)


def _closes(n, start=100.0, step=1.0):
    return pd.Series([start + step * i for i in range(n)])


# ===========================================================================
# A. the headline defect: a name with no data is bought in a falling market
# ===========================================================================

def test_a_symbol_with_too_little_history_is_not_ranked_mid_pack():
    """Three real names all falling, one IPO with 100 closes. The IPO must not
    win the ranking on a momentum it does not have."""
    prices = {
        "AAA": _closes(300, 200.0, -0.5),      # falling
        "BBB": _closes(300, 200.0, -0.4),      # falling
        "CCC": _closes(300, 200.0, -0.3),      # falling
        "IPO": _closes(100, 50.0, 0.1),        # too short to measure
    }
    mom = momentum_12_1(prices)
    assert mom["IPO"] is None, "an unmeasurable momentum must not be a number"

    comp = composite_score({"momentum": mom}, {"momentum": 1.0})
    ranked = rank_symbols(comp)
    targets = long_only_top_n(ranked, comp, top_n=2)
    assert "IPO" not in ranked
    assert "IPO" not in targets
    assert ranked == ["CCC", "BBB", "AAA"]     # the measured names, best-first


def test_the_defect_is_reachable_without_any_failure():
    """No exception, no outage: 251 closes is simply a young listing."""
    assert momentum_12_1({"X": _closes(251)})["X"] is None
    assert momentum_12_1({"X": _closes(252)})["X"] is not None


def test_momentum_of_a_flat_series_is_a_measured_zero():
    """The inverse error: a genuinely flat 12-1 window IS 0.0."""
    assert momentum_12_1({"X": pd.Series([100.0] * 300)})["X"] == 0.0


def test_momentum_with_a_non_positive_start_price_is_unknown():
    s = pd.Series([0.0] * 60 + [10.0] * 240)   # the -252nd close is 0
    assert momentum_12_1({"X": s})["X"] is None


# ===========================================================================
# B. the composite: renormalize over what was measured, record n_factors
# ===========================================================================

_THREE = {"momentum": 1.0, "value": 1.0, "quality": 1.0}


def _three_factor_universe():
    """AAA/BBB/CCC measured on all three; DDD missing quality."""
    return {
        "momentum": {"AAA": 0.30, "BBB": 0.10, "CCC": -0.10, "DDD": 0.20},
        "value":    {"AAA": 0.02, "BBB": 0.06, "CCC": 0.10, "DDD": 0.08},
        "quality":  {"AAA": 0.40, "BBB": 0.20, "CCC": 0.10, "DDD": None},
    }


def test_composite_renormalises_a_partly_measured_symbol_over_its_own_factors():
    values = _three_factor_universe()
    z = {n: cross_sectional_zscore(v) for n, v in values.items()}
    got = composite_score(values, _THREE, min_factors=2)
    # DDD: measured on momentum + value only -> the 2 measured contributions
    # scaled back up to the full weight of 3.
    expected = (z["momentum"]["DDD"] + z["value"]["DDD"]) / 2.0 * 3.0
    assert got["DDD"] == pytest.approx(expected)


def test_composite_is_unchanged_for_a_fully_measured_symbol():
    """Renormalizing must be a no-op when nothing is missing, or every stored
    book and every backtest result silently shifts."""
    values = _three_factor_universe()
    z = {n: cross_sectional_zscore(v) for n, v in values.items()}
    got = composite_score(values, _THREE, min_factors=2)
    for sym in ("AAA", "BBB", "CCC"):
        assert got[sym] == pytest.approx(sum(z[n][sym] for n in _THREE))


def test_composite_detail_records_how_many_factors_were_measured():
    detail = composite_detail(_three_factor_universe(), _THREE, min_factors=2)
    assert detail["AAA"]["n_factors"] == 3
    assert detail["DDD"]["n_factors"] == 2
    assert detail["DDD"]["n_weighted"] == 3


def test_a_symbol_below_K_measured_factors_is_excluded_with_a_reason():
    values = {
        "momentum": {"AAA": 0.30, "BBB": 0.10, "THIN": 0.20},
        "value":    {"AAA": 0.02, "BBB": 0.06, "THIN": None},
        "quality":  {"AAA": 0.40, "BBB": 0.20, "THIN": None},
    }
    detail = composite_detail(values, _THREE, min_factors=2)
    assert detail["THIN"]["score"] is None
    assert detail["THIN"]["n_factors"] == 1
    assert "1 of 3" in detail["THIN"]["detail"]
    assert "THIN" not in composite_score(values, _THREE, min_factors=2)


def test_a_symbol_measured_on_nothing_is_always_excluded():
    values = {"momentum": {"AAA": 0.3, "GHOST": None},
              "value": {"AAA": 0.02, "GHOST": None}}
    for k in (0, 1, 2):
        # even min_factors=0 must not admit a name with nothing behind it: the
        # coverage bar is clamped to at least 1.
        detail = composite_detail(values, {"momentum": 1.0, "value": 1.0}, min_factors=k)
        assert detail["GHOST"]["score"] is None, f"min_factors={k}"
        assert detail["GHOST"]["n_factors"] == 0


def test_the_symbol_order_is_sorted_so_the_book_is_reproducible():
    """A raw set union iterates in a PYTHONHASHSEED-dependent order."""
    values = {"momentum": {"ZZZ": 0.1, "AAA": 0.2, "MMM": 0.3},
              "value": {"ZZZ": 0.1, "AAA": 0.2, "MMM": 0.3}}
    assert list(composite_detail(values, {"momentum": 1.0, "value": 1.0})) == \
        ["AAA", "MMM", "ZZZ"]


def test_K_is_clamped_to_the_number_of_weighted_factors():
    """A deliberately single-factor model must still produce a book."""
    values = {"momentum": {"AAA": 0.3, "BBB": 0.1, "CCC": -0.2}}
    comp = composite_score(values, {"momentum": 1.0})
    assert set(comp) == {"AAA", "BBB", "CCC"}


def test_a_symbol_absent_from_a_factor_dict_counts_as_unmeasured_not_zero():
    """The fetchers DROP symbols they cannot compute, so absence is the common
    unmeasurable case -- and ``z.get(s, 0.0)`` used to score it as average."""
    values = {
        "momentum": {"AAA": 0.30, "BBB": 0.10, "CCC": -0.10, "GONE": 0.20},
        "value":    {"AAA": 0.02, "BBB": 0.06, "CCC": 0.10},   # GONE dropped
        "quality":  {"AAA": 0.40, "BBB": 0.20, "CCC": 0.10},   # GONE dropped
    }
    detail = composite_detail(values, _THREE, min_factors=2)
    assert detail["GONE"]["n_factors"] == 1
    assert detail["GONE"]["score"] is None


def test_zero_weight_factors_do_not_count_toward_coverage():
    values = {
        "momentum": {"AAA": 0.30, "BBB": 0.10},
        "value":    {"AAA": 0.02, "BBB": 0.06},
        "quality":  {"AAA": None, "BBB": 0.20},
    }
    # quality disabled -> only 2 weighted factors -> K clamps to 2, and AAA's
    # missing quality is irrelevant because quality carries no weight.
    comp = composite_score(values, {"momentum": 1.0, "value": 1.0, "quality": 0.0},
                           min_factors=2)
    assert set(comp) == {"AAA", "BBB"}
    detail = composite_detail(values, {"momentum": 1.0, "value": 1.0, "quality": 0.0},
                              min_factors=2)
    assert detail["BBB"]["n_weighted"] == 2      # quality is off, so it is not "weighted"
    assert detail["BBB"]["n_factors"] == 2       # ... and its measured z does not count


def test_the_default_K_is_two():
    assert DEF_MIN_MEASURED_FACTORS == 2


# ===========================================================================
# C. the cross-section: an unmeasured value is not part of the comparator
# ===========================================================================

def test_the_zscore_comparator_excludes_unmeasured_names():
    z = cross_sectional_zscore({"AAA": 1.0, "BBB": 3.0, "GHOST": None})
    assert z["GHOST"] is None
    # mean/sd over {1, 3} only -- GHOST must not drag the mean to 4/3.
    assert z["AAA"] == pytest.approx(-1.0)
    assert z["BBB"] == pytest.approx(1.0)


def test_the_stats_count_only_the_measured_names():
    st = cross_sectional_stats({"AAA": 1.0, "BBB": 3.0, "GHOST": None})
    assert st["n"] == 2
    assert st["n_unmeasured"] == 1
    assert st["mean"] == pytest.approx(2.0)


def test_an_all_unmeasured_cross_section_is_degenerate_with_no_mean():
    st = cross_sectional_stats({"A": None, "B": None})
    assert st["n"] == 0
    assert st["mean"] is None
    assert st["degenerate"] is True


def test_a_nan_is_unmeasured_too():
    """NaN is how pandas/the metric store spell "no value". Letting one into the
    array makes mean and sd NaN, ``sd > 0`` False, and the "no dispersion" branch
    then returns 0.0 for EVERY symbol -- one missing name flattens the whole factor."""
    z = cross_sectional_zscore({"AAA": 1.0, "BBB": 3.0, "NAN": float("nan")})
    assert z["NAN"] is None
    assert z["AAA"] == pytest.approx(-1.0)
    assert z["BBB"] == pytest.approx(1.0)
    assert cross_sectional_stats({"AAA": 1.0, "BBB": 3.0, "NAN": float("nan")})["n"] == 2


def test_a_measured_zero_stays_in_the_cross_section():
    """The inverse error: 0.0 is a value, and dropping it would move the mean."""
    z = cross_sectional_zscore({"AAA": -1.0, "ZERO": 0.0, "BBB": 1.0})
    assert z["ZERO"] == pytest.approx(0.0)
    assert cross_sectional_stats({"AAA": -1.0, "ZERO": 0.0, "BBB": 1.0})["n"] == 3


# ===========================================================================
# D. the per-factor calculators
# ===========================================================================

def test_quality_renormalises_over_its_measured_terms():
    """roe + gp/ta - accruals are three equally-weighted ratios; a name missing
    one is scored on the two it has, not penalised by a fabricated 0."""
    full = quality_score({"X": {"roe": 0.3, "gross_profit": 30.0,
                                "total_assets": 100.0, "accruals_ratio": 0.0}})["X"]
    assert full == pytest.approx((0.3 + 0.3 - 0.0) / 3.0)
    partial = quality_score({"X": {"roe": 0.3, "gross_profit": 30.0,
                                   "total_assets": 100.0}})["X"]
    assert partial == pytest.approx((0.3 + 0.3) / 2.0)


def test_quality_with_no_measurable_term_is_unknown():
    assert quality_score({"X": {}})["X"] is None
    assert quality_score({"X": {"gross_profit": 30.0}})["X"] is None   # no total_assets


def test_quality_rejects_a_non_positive_asset_base():
    """Gross profitability over zero (or negative) assets is not a small number,
    it is not a number."""
    assert quality_score({"X": {"gross_profit": 30.0, "total_assets": 0.0}})["X"] is None
    assert quality_score({"X": {"gross_profit": 30.0, "total_assets": -100.0}})["X"] is None


def test_quality_of_exactly_zero_is_a_measurement():
    q = quality_score({"X": {"roe": 0.1, "gross_profit": 0.0, "total_assets": 100.0,
                             "accruals_ratio": 0.1}})["X"]
    assert q == pytest.approx(0.0)
    assert q is not None


def test_value_renormalises_over_its_measured_legs():
    both = value_score({"X": {"eps_ttm": 5.0, "price": 50.0,
                              "fcf_ttm": 10.0, "enterprise_value": 100.0}})["X"]
    assert both == pytest.approx((0.1 + 0.1) / 2.0)
    ey_only = value_score({"X": {"eps_ttm": 5.0, "price": 50.0}})["X"]
    assert ey_only == pytest.approx(0.1)


def test_value_with_no_measurable_leg_is_unknown():
    assert value_score({"X": {}})["X"] is None
    assert value_score({"X": {"eps_ttm": 5.0, "price": 0.0}})["X"] is None
    assert value_score({"X": {"fcf_ttm": 10.0, "enterprise_value": 0.0}})["X"] is None


def test_value_rejects_negative_denominators():
    """A negative price is a corrupt field; a negative enterprise value (net cash
    above market cap plus debt) makes the FCF yield's sign meaningless."""
    assert value_score({"X": {"eps_ttm": 5.0, "price": -50.0}})["X"] is None
    assert value_score({"X": {"fcf_ttm": 10.0, "enterprise_value": -100.0}})["X"] is None


def test_a_zero_earnings_yield_is_a_measurement_not_a_gap():
    """eps_ttm of exactly 0 is a company that broke even, not a missing field."""
    v = value_score({"X": {"eps_ttm": 0.0, "price": 50.0}})["X"]
    assert v == pytest.approx(0.0)
    assert v is not None


def test_pead_outside_the_drift_window_is_a_measured_zero():
    """The factor is DEFINED as zero outside the window -- there is no drift to
    measure, which is a statement about the world, not about our data."""
    out = earnings_surprise({"X": {"actual": 1.5, "estimate": 1.0,
                                   "estimate_std": 0.1, "days_since": 90}},
                            drift_window_days=60)
    assert out["X"] == 0.0


def test_pead_without_a_report_date_is_unknown():
    out = earnings_surprise({"X": {"actual": 1.5, "estimate": 1.0,
                                   "estimate_std": 0.1, "days_since": None}})
    assert out["X"] is None


def test_pead_without_a_dispersion_std_is_unknown():
    """SUE cannot be standardized without a dispersion -- that is unknown, and
    it used to read as 'this company reported exactly in line'."""
    assert earnings_surprise({"X": {"actual": 1.5, "estimate": 1.0,
                                    "estimate_std": None, "days_since": 5}})["X"] is None
    assert earnings_surprise({"X": {"actual": 1.5, "estimate": 1.0,
                                    "estimate_std": 0.0, "days_since": 5}})["X"] is None


def test_pead_missing_the_actual_or_the_estimate_is_unknown():
    assert earnings_surprise({"X": {"estimate": 1.0, "estimate_std": 0.1,
                                    "days_since": 5}})["X"] is None
    assert earnings_surprise({"X": {"actual": 1.0, "estimate_std": 0.1,
                                    "days_since": 5}})["X"] is None


def test_a_pead_surprise_of_exactly_zero_is_a_measurement():
    out = earnings_surprise({"X": {"actual": 1.0, "estimate": 1.0,
                                   "estimate_std": 0.1, "days_since": 5}})
    assert out["X"] == 0.0


# ===========================================================================
# E. the ranking of a fully-measured universe is untouched
# ===========================================================================

def test_a_fully_measured_universe_ranks_exactly_as_the_old_formula_did():
    """Renormalizing rescales quality/value by 1/n_terms, and a cross-sectional
    z-score is invariant to a positive affine rescale -- so a book with complete
    data must come out bit-identical. Without this the fix is a silent
    behaviour change to every backtest."""
    raw_q = {"AAA": {"roe": 0.30, "gross_profit": 30.0, "total_assets": 100.0,
                     "accruals_ratio": 0.01},
             "BBB": {"roe": 0.10, "gross_profit": 12.0, "total_assets": 100.0,
                     "accruals_ratio": 0.05},
             "CCC": {"roe": 0.05, "gross_profit": 40.0, "total_assets": 100.0,
                     "accruals_ratio": 0.02}}
    old_quality = {s: (d["roe"] + d["gross_profit"] / d["total_assets"]
                       - d["accruals_ratio"]) for s, d in raw_q.items()}
    new_quality = quality_score(raw_q)
    old_comp = composite_score({"quality": old_quality}, {"quality": 1.0})
    new_comp = composite_score({"quality": new_quality}, {"quality": 1.0})
    assert rank_symbols(new_comp) == rank_symbols(old_comp)
    for s in old_comp:
        assert new_comp[s] == pytest.approx(old_comp[s])


# ===========================================================================
# F. the ranked book and the SYMBOL360 card
# ===========================================================================

def _expert(symbol=None):
    e = FactorRanker.__new__(FactorRanker)
    if symbol:
        e._export_symbol = symbol
    e.logger = logging.getLogger("test.FactorRanker.unmeasurable")
    return e


def _book_with_one_excluded(held=None):
    values = {
        "momentum": {"AAA": 0.30, "BBB": 0.10, "THIN": 0.20},
        "value":    {"AAA": 0.02, "BBB": 0.06, "THIN": None},
        "quality":  {"AAA": 0.40, "BBB": 0.20, "THIN": None},
    }
    detail = composite_detail(values, _THREE, min_factors=2)
    comp = {s: d["score"] for s, d in detail.items() if d["score"] is not None}
    ranked = rank_symbols(comp)
    targets = long_only_top_n(ranked, comp, top_n=1)
    book = _expert()._build_book(ranked, comp, values, targets, _THREE, 0.0,
                                 held=held or set(), gross_exposure=1.0,
                                 detail=detail)
    return book


def test_the_book_lists_an_excluded_symbol_with_no_rank_and_no_target():
    book = _book_with_one_excluded()
    row = next(r for r in book["ranking"] if r["symbol"] == "THIN")
    assert row["composite"] is None
    assert row["rank"] is None
    assert row["target_weight"] == 0.0
    assert "1 of 3" in row["excluded_reason"]


def test_the_book_stores_an_unmeasured_factor_as_null_not_zero():
    """MarketAnalysis.state is what the UI and every later reader see; rounding a
    None to 0.0 on the way in puts the fabricated zero back permanently."""
    book = _book_with_one_excluded()
    thin = next(r for r in book["ranking"] if r["symbol"] == "THIN")
    assert thin["factors_raw"]["value"] is None
    assert thin["factors_raw"]["quality"] is None
    assert thin["factors"]["value"] is None
    assert thin["factors_raw"]["momentum"] == pytest.approx(0.20)   # measured, kept


def test_a_ranked_row_with_one_missing_factor_stores_null_for_it():
    values = {
        "momentum": {"AAA": 0.30, "BBB": 0.10, "CCC": -0.05},
        "value":    {"AAA": 0.02, "BBB": 0.06, "CCC": 0.10},
        "quality":  {"AAA": 0.40, "BBB": 0.20, "CCC": None},
    }
    detail = composite_detail(values, _THREE, min_factors=2)
    comp = {s: d["score"] for s, d in detail.items() if d["score"] is not None}
    ranked = rank_symbols(comp)
    book = _expert()._build_book(ranked, comp, values, {}, _THREE, 0.0,
                                 gross_exposure=1.0, detail=detail)
    ccc = next(r for r in book["ranking"] if r["symbol"] == "CCC")
    assert ccc["rank"] is not None            # measured on 2 of 3 -> still ranked
    assert ccc["composite"] is not None
    assert ccc["factors_raw"]["quality"] is None
    assert ccc["n_factors"] == 2


def test_the_book_records_n_factors_per_symbol():
    book = _book_with_one_excluded()
    by_sym = {r["symbol"]: r for r in book["ranking"]}
    assert by_sym["AAA"]["n_factors"] == 3
    assert by_sym["THIN"]["n_factors"] == 1


def test_an_excluded_holding_is_still_marked_SELL():
    """A held name that drops out of the ranking must be exited, not orphaned."""
    book = _book_with_one_excluded(held={"THIN"})
    row = next(r for r in book["ranking"] if r["symbol"] == "THIN")
    assert row["action"] == "SELL"


def test_the_book_does_not_report_an_excluded_name_as_ranked():
    book = _book_with_one_excluded()
    assert book["universe_size"] == 2
    assert book["excluded_count"] == 1


def test_an_unmeasured_raw_factor_is_not_rendered_as_plus_zero():
    """SYMBOL360's per-factor rows are the numbers a single-symbol card actually
    shows (its composite is always n/a); an unmeasured one must not print
    +0.0000 there either."""
    rec = Recommendation(
        OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked book",
        raw_outputs={"book": {"universe_size": 1, "ranking": [
            {"symbol": "IPO", "rank": 1, "composite": 0.0,
             "factors": {"momentum": None, "value": 0.0},
             "factors_raw": {"momentum": None, "value": 0.05},
             "n_factors": 1, "target_weight": 1.0, "action": "BUY"}]}})
    out = _expert("IPO")._build_export_metrics(rec, {})
    mom = next(m for m in out if m.label == "Factor: momentum (raw)")
    assert mom.value is None
    assert mom.display == "n/a"
    assert "not measurable" in mom.detail.lower() or "could not be measured" in mom.detail.lower()


def test_a_measured_zero_raw_factor_is_still_rendered_as_a_number():
    rec = Recommendation(
        OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked book",
        raw_outputs={"book": {"universe_size": 1, "ranking": [
            {"symbol": "FLAT", "rank": 1, "composite": 0.0,
             "factors": {"momentum": 0.0}, "factors_raw": {"momentum": 0.0},
             "n_factors": 1, "target_weight": 1.0, "action": "BUY"}]}})
    out = _expert("FLAT")._build_export_metrics(rec, {})
    mom = next(m for m in out if m.label == "Factor: momentum (raw)")
    assert mom.value == 0.0
    assert mom.display == "+0.0000"


def test_the_card_explains_that_an_excluded_symbol_was_not_scored():
    rec = Recommendation(
        OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked book",
        raw_outputs={"book": {"universe_size": 4, "cross_section": {
            "universe_size": 4, "weights": {"momentum": 1.0, "value": 1.0},
            "factors": {"momentum": {"n": 4, "mean": 0.1, "sd": 0.2, "degenerate": False},
                        "value": {"n": 3, "mean": 0.05, "sd": 0.02, "degenerate": False}}},
            "ranking": [
                {"symbol": "THIN", "rank": None, "composite": None,
                 "factors": {"momentum": 0.5, "value": None},
                 "factors_raw": {"momentum": 0.2, "value": None},
                 "n_factors": 1, "target_weight": 0.0, "action": "—",
                 "excluded_reason": "measured on 1 of 2 weighted factors "
                                    "(needs 2) — not ranked"}]}})
    out = _expert("THIN")._build_export_metrics(rec, {})
    row = next(m for m in out if m.label == "Composite factor score")
    assert row.value is None
    assert row.display == "n/a"
    assert "1 of 2" in row.detail


def test_a_null_composite_is_never_rendered_as_a_number_even_without_a_reason():
    """Belt and braces: a stored row whose composite is null but which carries no
    exclusion reason (a book written by an older build) must still show n/a. The
    cross-section here has real dispersion, so the availability check says
    'available' and only the None guard stands between the card and +0.000."""
    rec = Recommendation(
        OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked book",
        raw_outputs={"book": {"universe_size": 3, "cross_section": {
            "universe_size": 3, "weights": {"momentum": 1.0},
            "factors": {"momentum": {"n": 3, "mean": 0.1, "sd": 0.2,
                                     "degenerate": False}}},
            "ranking": [
                {"symbol": "AAA", "rank": 1, "composite": 1.0,
                 "factors": {"momentum": 1.0}, "factors_raw": {"momentum": 0.3},
                 "target_weight": 1.0, "action": "BUY"},
                {"symbol": "BBB", "rank": 2, "composite": 0.0,
                 "factors": {"momentum": 0.0}, "factors_raw": {"momentum": 0.1},
                 "target_weight": 0.0, "action": "—"},
                {"symbol": "ODD", "rank": None, "composite": None,
                 "factors": {"momentum": None}, "factors_raw": {"momentum": None},
                 "target_weight": 0.0, "action": "—"}]}})
    row = next(m for m in _expert("ODD")._build_export_metrics(rec, {})
               if m.label == "Composite factor score")
    assert row.value is None
    assert row.display == "n/a"


# ===========================================================================
# G. the live/backtest wiring: _gather, the metric-store fast path, _process
# ===========================================================================

_AS_OF = datetime(2024, 3, 15, tzinfo=timezone.utc)     # frozen, and not today


class _StubBundle:
    """The two-method slice of ProviderBundle that _gather touches."""

    def ohlcv(self):
        return None


def test_the_metric_store_fast_path_keeps_a_nan_momentum_unknown(monkeypatch):
    """The store writes NaN where the 252-bar window had not filled. ``or 0.0``
    turned that into the best momentum in the cross-section -- and only on the
    fast path, so the store-backed run disagreed with the OHLCV run."""
    from ba2_providers.screener import metric_store as ms
    monkeypatch.setattr(ms, "load_store", lambda name: "df")
    monkeypatch.setattr(ms, "metrics_as_of", lambda df, d, cols: {
        "AAA": {"momentum_12_1": 0.30, "close": 10.0},
        "IPO": {"momentum_12_1": float("nan"), "close": 5.0}})

    e = _expert()
    e.get_setting_with_interface_default = lambda key, **kw: "store.parquet"
    momentum, price_as_of = e._store_factor_inputs(["AAA", "IPO"], _AS_OF)
    assert momentum == {"AAA": 0.30, "IPO": None}
    assert price_as_of == {"AAA": 10.0, "IPO": 5.0}


def test_gather_keeps_a_symbol_the_store_cannot_price_as_unknown(monkeypatch):
    e = _expert()
    e._gather_settings = {"_factor_weights": {"momentum": 1.0, "value": 0.0,
                                              "quality": 0.0, "pead": 0.0},
                          "pead_drift_window_days": 60}
    monkeypatch.setattr(FactorRanker, "_resolve_universe",
                        lambda self, as_of: ["AAA", "IPO"])
    monkeypatch.setattr(FactorRanker, "_store_factor_inputs",
                        lambda self, u, a: ({"AAA": 0.30}, None))
    monkeypatch.setattr(FactorRanker, "_gather_holdings", lambda self: [])
    bundle = e._gather(_StubBundle(), _AS_OF)
    assert bundle["factors"]["momentum"] == {"AAA": 0.30, "IPO": None}


def _process_bundle(factors, holdings=()):
    return {"universe": sorted({s for v in factors.values() for s in v}),
            "factors": factors, "holdings": list(holdings), "prices": {},
            "current_price": None}


_PROCESS_SETTINGS = {"_factor_weights": _THREE, "winsorize_pct": 0.0, "top_n": 5,
                     "weighting": "equal", "max_weight_per_name": 1.0,
                     "gross_exposure": 1.0}


def test_process_never_targets_a_name_it_could_not_score():
    """End of the pipe: the unrankable name must not reach ``targets``, which is
    what the portfolio manager actually trades."""
    factors = {
        "momentum": {"AAA": 0.30, "BBB": 0.10, "CCC": -0.10, "THIN": 0.40},
        "value":    {"AAA": 0.02, "BBB": 0.06, "CCC": 0.10, "THIN": None},
        "quality":  {"AAA": 0.40, "BBB": 0.20, "CCC": 0.10, "THIN": None},
    }
    rec = _expert()._process(_process_bundle(factors), dict(_PROCESS_SETTINGS))
    targets = rec.raw_outputs["targets"]
    book = rec.raw_outputs["book"]
    assert "THIN" not in targets
    assert [r["symbol"] for r in book["ranking"] if r["rank"] is not None] == \
        ["AAA", "BBB", "CCC"]
    assert book["excluded_count"] == 1


def test_process_still_holds_a_name_measured_on_most_of_the_model():
    factors = {
        "momentum": {"AAA": 0.30, "BBB": 0.10, "PART": 0.40},
        "value":    {"AAA": 0.02, "BBB": 0.06, "PART": 0.09},
        "quality":  {"AAA": 0.40, "BBB": 0.20, "PART": None},
    }
    rec = _expert()._process(_process_bundle(factors), dict(_PROCESS_SETTINGS))
    assert "PART" in rec.raw_outputs["targets"]
