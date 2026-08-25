"""The FactorRanker composite is NOT COMPUTABLE in a single-symbol view.

``composite_score`` is a weighted sum of CROSS-SECTIONAL z-scores, and
``cross_sectional_zscore`` returns zeros whenever the cross-section has no
dispersion (``sd > 0`` is false). SYMBOL360 pins the universe to exactly the
requested symbol, so sd is 0 by construction and the composite is forced to
+0.000 for every symbol, always.

+0.000 is not a neutral reading, it is "no answer": rendering it as a number
invites the user to read "no signal", which is a different and false
statement. This file (a) PROVES the degeneracy rather than assuming it, and
(b) pins the presentation -- unavailable, with the reason -- while protecting
the inverse error: a genuinely measured composite that happens to equal 0.0
must still render as a number.
"""
import logging

import pytest

from ba2_common.core.types import OrderRecommendation, Recommendation
from ba2_experts.FactorRanker import FactorRanker
from ba2_experts.FactorRanker.factors import (
    composite_score, cross_sectional_stats, cross_sectional_zscore,
    describe_composite_availability,
)


# --------------------------------------------------------------------------
# (a) Verify the reading before acting on it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [3.0399, -0.0297, -0.0515, 0.0, 1e9, -1e9])
def test_a_one_symbol_cross_section_z_scores_to_zero_whatever_the_value(raw):
    assert cross_sectional_zscore({"AAPL": raw}) == {"AAPL": 0.0}


@pytest.mark.parametrize("values", [
    {"momentum": {"AAPL": 3.0399}, "value": {"AAPL": -0.0297}, "quality": {"AAPL": -0.0515}},
    {"momentum": {"AAPL": -50.0}, "value": {"AAPL": 12.5}, "quality": {"AAPL": 0.9}},
])
def test_the_single_symbol_composite_is_structurally_zero(values):
    """The exact numbers off the reported card, and a wildly different set:
    same answer. The composite carries no information here."""
    weights = {"momentum": 0.4, "value": 0.3, "quality": 0.3}
    assert composite_score(values, weights) == {"AAPL": 0.0}


def test_more_than_one_symbol_with_real_dispersion_does_produce_a_composite():
    """Control: the degeneracy is about the universe size, not the code being
    broken. Without this the fix could 'work' by never computing anything."""
    values = {"value": {"AAA": 0.10, "BBB": 0.02, "CCC": -0.04}}
    comp = composite_score(values, {"value": 1.0})
    assert comp["AAA"] > 0 > comp["CCC"]


# --------------------------------------------------------------------------
# cross_sectional_stats -- the evidence behind the availability decision
# --------------------------------------------------------------------------

def test_cross_sectional_stats_flags_a_single_symbol_as_degenerate():
    st = cross_sectional_stats({"AAPL": 3.0399})
    assert st["n"] == 1
    assert st["sd"] == 0.0
    assert st["degenerate"] is True


def test_cross_sectional_stats_flags_a_flat_multi_symbol_cross_section_as_degenerate():
    st = cross_sectional_stats({"AAA": 2.0, "BBB": 2.0, "CCC": 2.0})
    assert st["n"] == 3
    assert st["sd"] == 0.0
    assert st["degenerate"] is True


def test_cross_sectional_stats_reports_the_comparator_actually_used():
    st = cross_sectional_stats({"AAA": 1.0, "BBB": 3.0})
    assert st["n"] == 2
    assert st["mean"] == pytest.approx(2.0)
    assert st["sd"] == pytest.approx(1.0)
    assert st["degenerate"] is False


def test_cross_sectional_stats_reports_the_WINSORIZED_comparator():
    """Winsorizing happens before standardizing, so the mean/sd shown must be
    the post-winsorize ones -- otherwise the displayed comparator is not the
    one the z-score was actually taken against."""
    vals = {f"S{i}": float(i) for i in range(10)}
    vals["OUT"] = 1000.0
    plain = cross_sectional_stats(vals)
    wins = cross_sectional_stats(vals, winsorize_pct=0.1)
    assert wins["sd"] < plain["sd"]
    assert wins["winsorize_pct"] == 0.1


def test_cross_sectional_stats_matches_the_zscore_it_explains():
    """Mutation guard: the stats must be the SAME numbers the z-score used."""
    vals = {"AAA": 1.0, "BBB": 3.0, "CCC": 8.0}
    st = cross_sectional_stats(vals)
    z = cross_sectional_zscore(vals)
    for sym, v in vals.items():
        assert z[sym] == pytest.approx((v - st["mean"]) / st["sd"])


# --------------------------------------------------------------------------
# describe_composite_availability -- pure
# --------------------------------------------------------------------------

def test_a_one_symbol_universe_is_unavailable_and_says_why():
    ok, reason = describe_composite_availability(
        1, {"value": {"n": 1, "sd": 0.0, "degenerate": True}}, {"value": 1.0})
    assert ok is False
    assert "1 symbol" in reason
    assert "cross-sectional" in reason.lower()


def test_a_flat_multi_symbol_universe_is_unavailable_and_names_the_size():
    ok, reason = describe_composite_availability(
        3, {"value": {"n": 3, "sd": 0.0, "degenerate": True}}, {"value": 1.0})
    assert ok is False
    assert "3" in reason
    assert "dispersion" in reason.lower()


def test_a_universe_with_dispersion_is_available():
    ok, reason = describe_composite_availability(
        3, {"value": {"n": 3, "sd": 0.7, "degenerate": False}}, {"value": 1.0})
    assert ok is True
    assert reason is None


def test_a_zero_weight_factor_cannot_rescue_the_composite():
    """Only factors that actually CONTRIBUTE decide availability."""
    ok, _ = describe_composite_availability(
        3,
        {"value": {"n": 3, "sd": 0.0, "degenerate": True},
         "momentum": {"n": 3, "sd": 5.0, "degenerate": False}},
        {"value": 1.0, "momentum": 0.0})
    assert ok is False


def test_with_no_per_factor_stats_the_universe_size_still_decides():
    """Legacy books (stored MarketAnalysis.state from before this change) carry
    no per-factor stats; a 1-symbol universe must still report unavailable."""
    assert describe_composite_availability(1, {}, {"value": 1.0})[0] is False
    assert describe_composite_availability(4, {}, {"value": 1.0})[0] is True


# --------------------------------------------------------------------------
# (b) The rendered rows
# --------------------------------------------------------------------------

def _expert(symbol="AAPL"):
    e = FactorRanker.__new__(FactorRanker)
    e._export_symbol = symbol
    e.logger = logging.getLogger("test.FactorRanker.export")
    return e


def _rec(ranking, cross_section=None, universe_size=None):
    book = {"universe_size": universe_size if universe_size is not None else len(ranking),
            "ranking": ranking}
    if cross_section is not None:
        book["cross_section"] = cross_section
    return Recommendation(OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked book",
                          raw_outputs={"book": book})


_SINGLE = [{"symbol": "AAPL", "rank": 1, "composite": 0.0,
            "factors": {"momentum": 0.0, "value": 0.0, "quality": 0.0},
            "factors_raw": {"momentum": 3.0399, "value": -0.0297, "quality": -0.0515},
            "target_weight": 1.0, "action": "BUY"}]
_SINGLE_XS = {"universe_size": 1, "weights": {"momentum": 0.4, "value": 0.3, "quality": 0.3},
              "factors": {n: {"n": 1, "mean": v, "sd": 0.0, "degenerate": True}
                          for n, v in (("momentum", 3.0399), ("value", -0.0297),
                                       ("quality", -0.0515))}}


def test_the_single_symbol_composite_is_never_rendered_as_a_number():
    out = _expert()._build_export_metrics(_rec(_SINGLE, _SINGLE_XS), {})
    row = next(m for m in out if m.label == "Composite factor score")
    assert row.value is None, "a not-computable composite must not carry a numeric value"
    assert "0.000" not in row.display
    assert row.display == "n/a"
    assert row.signal is None


def test_the_unavailable_composite_states_the_reason():
    out = _expert()._build_export_metrics(_rec(_SINGLE, _SINGLE_XS), {})
    row = next(m for m in out if m.label == "Composite factor score")
    assert row.detail
    assert "1 symbol" in row.detail
    assert "cross-sectional" in row.detail.lower()


def test_the_composite_row_carries_the_per_factor_evidence_as_a_table():
    """Even when the composite itself is n/a, the row must still show WHAT was
    measured and against what -- otherwise "unavailable" is just a shrug."""
    out = _expert()._build_export_metrics(_rec(_SINGLE, _SINGLE_XS), {})
    table = dict(next(m for m in out if m.label == "Composite factor score").detail_table)
    assert table["momentum: raw"] == "+3.0399"
    assert "sd 0 (no dispersion)" in table["momentum: compared against"]
    assert table["momentum: z × weight"] == "n/a (sd 0) × 0.40"


def test_a_computable_composite_row_shows_the_real_comparator_and_z():
    out = _expert("AAA")._build_export_metrics(_rec(_MULTI, _MULTI_XS), {})
    table = dict(next(m for m in out if m.label == "Composite factor score").detail_table)
    assert table["value: raw"] == "+0.1000"
    assert "mean +0.0400" in table["value: compared against"]
    assert "sd 0.0490" in table["value: compared against"]
    assert table["value: z × weight"] == "+1.2247 × 1.00"


def test_the_raw_factor_values_survive_and_are_labelled_raw():
    out = _expert()._build_export_metrics(_rec(_SINGLE, _SINGLE_XS), {})
    mom = next(m for m in out if m.label == "Factor: momentum (raw)")
    assert mom.value == pytest.approx(3.0399)
    assert mom.display == "+3.0399"
    assert mom.signal is None
    assert "raw" in mom.detail.lower()
    assert "not comparable" in mom.detail.lower() or "un-comparable" in mom.detail.lower()


def test_the_raw_factor_detail_says_they_are_not_on_a_common_scale():
    """+3.0399 momentum and -0.0297 value are different units entirely."""
    out = _expert()._build_export_metrics(_rec(_SINGLE, _SINGLE_XS), {})
    mom = next(m for m in out if m.label == "Factor: momentum (raw)")
    assert "scale" in mom.detail.lower()


# ---- the INVERSE error: do not suppress a real zero -----------------------

_MULTI = [
    {"symbol": "AAA", "rank": 1, "composite": 1.2247, "factors": {"value": 1.2247},
     "factors_raw": {"value": 0.10}, "target_weight": 1.0, "action": "BUY"},
    {"symbol": "BBB", "rank": 2, "composite": 0.0, "factors": {"value": 0.0},
     "factors_raw": {"value": 0.04}, "target_weight": 0.0, "action": "—"},
    {"symbol": "CCC", "rank": 3, "composite": -1.2247, "factors": {"value": -1.2247},
     "factors_raw": {"value": -0.02}, "target_weight": 0.0, "action": "—"},
]
_MULTI_XS = {"universe_size": 3, "weights": {"value": 1.0},
             "factors": {"value": {"n": 3, "mean": 0.04, "sd": 0.049, "degenerate": False}}}


def test_a_genuinely_measured_composite_of_exactly_zero_is_still_a_number():
    """BBB sits exactly on the cross-sectional mean: its composite really IS
    0.000. Suppressing that as 'unavailable' is the inverse error and just as
    wrong as printing an uncomputable 0."""
    out = _expert("BBB")._build_export_metrics(_rec(_MULTI, _MULTI_XS), {})
    row = next(m for m in out if m.label == "Composite factor score")
    assert row.value == 0.0
    assert row.display == "+0.000"


def test_a_measured_composite_explains_the_universe_it_was_ranked_against():
    out = _expert("AAA")._build_export_metrics(_rec(_MULTI, _MULTI_XS), {})
    row = next(m for m in out if m.label == "Composite factor score")
    assert row.value == pytest.approx(1.2247)
    assert row.detail and "3" in row.detail


def test_a_flat_multi_symbol_universe_is_also_reported_unavailable():
    """Three symbols with IDENTICAL raw values: sd is 0, so every z -- and the
    composite -- is forced to 0 exactly as in the 1-symbol case."""
    flat = [{"symbol": s, "rank": i + 1, "composite": 0.0, "factors": {"value": 0.0},
             "factors_raw": {"value": 0.04}, "target_weight": 0.0, "action": "—"}
            for i, s in enumerate(("AAA", "BBB", "CCC"))]
    xs = {"universe_size": 3, "weights": {"value": 1.0},
          "factors": {"value": {"n": 3, "mean": 0.04, "sd": 0.0, "degenerate": True}}}
    out = _expert("BBB")._build_export_metrics(_rec(flat, xs), {})
    row = next(m for m in out if m.label == "Composite factor score")
    assert row.value is None
    assert row.display == "n/a"


def test_a_legacy_book_without_cross_section_stats_still_degrades_honestly():
    """Stored MarketAnalysis.state written before this change has no
    'cross_section' key; a 1-row ranking must still report unavailable rather
    than crash or print +0.000."""
    out = _expert()._build_export_metrics(_rec(_SINGLE), {})
    row = next(m for m in out if m.label == "Composite factor score")
    assert row.value is None
    assert row.display == "n/a"


def test_a_legacy_multi_row_book_without_stats_still_shows_the_number():
    out = _expert("AAA")._build_export_metrics(_rec(_MULTI), {})
    row = next(m for m in out if m.label == "Composite factor score")
    assert row.value == pytest.approx(1.2247)


# --------------------------------------------------------------------------
# The card header's "Confidence: 0.0%" -- the same defect one line up
# --------------------------------------------------------------------------

def test_factorranker_declares_that_it_computes_no_per_symbol_confidence():
    """Every FactorRanker Recommendation passes a LITERAL 0.0 confidence (grep
    `Recommendation(` in FactorRanker/__init__.py: three sites, all 0.0). It is
    a placeholder for a basket expert, not a measurement, so a card must not
    print "Confidence: 0.0%"."""
    assert FactorRanker.EXPORT_CONFIDENCE_UNAVAILABLE_REASON
    assert "confidence" in FactorRanker.EXPORT_CONFIDENCE_UNAVAILABLE_REASON.lower()


def test_the_export_reports_confidence_as_unavailable_not_zero(monkeypatch):
    from ba2_experts.FactorRanker import data
    import pandas as pd

    class _FakeOHLCV:
        def get_ohlcv_data(self, symbol=None, end_date=None, lookback_days=400, interval="1d"):
            return pd.DataFrame({"Close": [10.0, 11.0, 12.0]})

    monkeypatch.setattr(data, "fetch_value_inputs", lambda symbols, as_of=None: {
        s: {"eps_ttm": 10.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0}
        for s in symbols})
    result = FactorRanker.export_symbol_data(
        "AAPL",
        overrides={"instrument_selection_method": "static", "universe_source": "static",
                   "enabled_instruments": {"AAPL": {"enabled": True}},
                   "factor_weight_momentum": 0.0, "factor_weight_value": 1.0,
                   "factor_weight_quality": 0.0, "factor_weight_pead": 0.0,
                   "top_n": 1, "weighting": "equal", "max_weight_per_name": 1.0,
                   "gross_exposure": 1.0, "winsorize_pct": 0.0,
                   "pead_drift_window_days": 60},
        providers_resolver=lambda cat, name, **kw: {"ohlcv": _FakeOHLCV()}.get(cat))
    assert result.error is None, result.error
    assert result.confidence is None
    assert result.confidence_unavailable_reason


def test_factorranker_declares_that_its_signal_is_not_a_per_symbol_verdict():
    """_process's only non-skip Recommendation site passes a LITERAL
    OrderRecommendation.OVERWEIGHT, so the card's header badge said BUY for
    every symbol ever searched -- a constant dressed as an assessment."""
    assert FactorRanker.EXPORT_SIGNAL_UNAVAILABLE_REASON


def test_the_export_draws_no_badge_for_a_basket_expert(monkeypatch):
    from ba2_experts.FactorRanker import data
    import pandas as pd

    class _FakeOHLCV:
        def get_ohlcv_data(self, symbol=None, end_date=None, lookback_days=400, interval="1d"):
            return pd.DataFrame({"Close": [10.0, 11.0, 12.0]})

    monkeypatch.setattr(data, "fetch_value_inputs", lambda symbols, as_of=None: {
        s: {"eps_ttm": 10.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0}
        for s in symbols})
    result = FactorRanker.export_symbol_data(
        "AAPL",
        overrides={"instrument_selection_method": "static", "universe_source": "static",
                   "enabled_instruments": {"AAPL": {"enabled": True}},
                   "factor_weight_momentum": 0.0, "factor_weight_value": 1.0,
                   "factor_weight_quality": 0.0, "factor_weight_pead": 0.0,
                   "top_n": 1, "weighting": "equal", "max_weight_per_name": 1.0,
                   "gross_exposure": 1.0, "winsorize_pct": 0.0,
                   "pead_drift_window_days": 60},
        providers_resolver=lambda cat, name, **kw: {"ohlcv": _FakeOHLCV()}.get(cat))
    assert result.error is None, result.error
    assert result.overall_signal is None
    assert result.signal_unavailable_reason
