"""A macro regime that could not be measured must not read as a measured neutral.

``regime_composite`` renormalizes over the inputs that resolved. When NONE of them
resolve -- the cold-FRED-cache case that really happened on 2026-08-11, where every
series was absent and only the index trend was ever fed -- the divisor is 0 and the
function used to return ``{"score": 0.0, ...}``: a computed-looking number built from
nothing.

0.0 is not a free reading. In ``multiply`` mode it becomes an exposure multiplier of
``m_floor + (1 - m_floor) * (0 + 1) / 2`` = **0.625**, i.e. the book is deliberately
sized down by 37.5% on the strength of data that does not exist; in ``gate`` mode it is
a regime comfortably above ``macro_gate_min``, so the gate reports "the macro check
passed" when the macro check never ran.

The rule this file pins is ``option_lifecycle``'s: "we cannot measure this" and "this
is fine" must never be the same return value. ``score`` is ``None`` when nothing
resolved, and every consumer (``final_score``, the SYMBOL360 card) already knows what
to do with ``None``.

The inverse error matters just as much and is pinned here too: a regime whose measured
inputs genuinely average to 0.0 IS neutral, must keep scoring 0.0, and must keep
producing the 0.625 multiplier. Suppressing that would be the same lie in the other
direction.
"""
import pandas as pd
import pytest

from ba2_experts.DeterministicScorer import DeterministicScorer
from ba2_experts.DeterministicScorer import explain
from ba2_experts.DeterministicScorer.combine import final_score
from ba2_experts.DeterministicScorer.macro import (
    DEF_MW, credit_score, exposure_multiplier, regime_composite, trend_score,
)


COLD = {k: None for k in DEF_MW}          # every FRED series + the index trend missing


# ---------------------------------------------------------------------------
# the calculator
# ---------------------------------------------------------------------------

def test_cold_cache_regime_score_is_unknown_not_zero():
    """No input resolved -> the composite is unmeasurable, not neutral."""
    out = regime_composite(COLD)
    assert out["score"] is None
    assert out["n_inputs"] == 0
    assert out["components"] == {}


def test_empty_inputs_are_unknown_too():
    assert regime_composite({})["score"] is None


def test_all_weights_zero_is_unknown_not_zero():
    """Nothing was weighted, so nothing was measured -- the divisor is 0 either way."""
    out = regime_composite({"trend_index": 1.0}, {"trend_index": 0.0})
    assert out["score"] is None
    assert out["n_inputs"] == 0


def test_a_zero_weight_input_does_not_count_as_a_resolved_one():
    """n_inputs is the corroboration count the hard risk-off cutoff is gated on
    (exposure_multiplier's min_inputs_for_riskoff). An input carrying no weight
    contributes nothing and must not arm that cutoff."""
    out = regime_composite({"trend_index": -1.0, "vix": 1.0},
                           {"trend_index": 1.0, "vix": 0.0})
    assert out["n_inputs"] == 1
    assert out["score"] == pytest.approx(-1.0)
    assert exposure_multiplier(out["score"], n_inputs=out["n_inputs"]) > 0.0


def test_an_out_of_range_input_is_clipped_into_the_exposure_domain():
    """Every scorer is documented as [-1, +1]; a stand-in that breaks that must
    not push the exposure multiplier out of [0, 1]."""
    out = regime_composite({"vix": 7.0}, {"vix": 1.0})
    assert out["score"] == 1.0


def test_a_credit_series_with_no_dispersion_is_unknown():
    """Zero sigma means the z-score is undefined, not that spreads are average."""
    assert credit_score(pd.Series([3.0] * 80)) is None


def test_a_too_short_index_history_is_unknown_not_flat():
    """``trend_score`` is +-1 BY CONSTRUCTION, so a 0.0 out of it could only ever
    be a fabricated 'no trend' -- and it is the one input the degraded backtest
    still has, i.e. the one whose absence must not read as neutral."""
    assert trend_score(pd.Series([100.0] * 50), sma_period=200) is None
    assert trend_score(None) is None


def test_a_measurable_index_history_still_reports_its_trend():
    rising = pd.Series([float(100 + i) for i in range(260)])
    assert trend_score(rising, sma_period=200) == 1.0
    assert trend_score(rising.iloc[::-1].reset_index(drop=True), sma_period=200) == -1.0


def test_a_non_positive_index_sma_is_unknown():
    """A zero/negative SMA is a corrupt series, not a downtrend."""
    assert trend_score(pd.Series([0.0] * 260), sma_period=200) is None


# ---------------------------------------------------------------------------
# ... and the legitimate zero it must NOT swallow
# ---------------------------------------------------------------------------

def test_a_genuinely_neutral_regime_still_scores_exactly_zero():
    """Two measured inputs that cancel: this IS a neutral regime and stays 0.0."""
    out = regime_composite({"vix": 1.0, "credit": -1.0}, {"vix": 0.5, "credit": 0.5})
    assert out["score"] == 0.0
    assert out["n_inputs"] == 2


def test_a_single_measured_zero_input_still_scores_zero():
    """A lone input measured AT zero is a measurement, not a missing input."""
    out = regime_composite({"vix": 0.0}, {"vix": 1.0})
    assert out["score"] == 0.0
    assert out["n_inputs"] == 1


def test_a_genuinely_neutral_regime_keeps_its_0_625_multiplier():
    """The 0.625 exposure factor is correct when the regime was actually measured."""
    neutral = regime_composite({"vix": 1.0, "credit": -1.0},
                               {"vix": 0.5, "credit": 0.5})
    assert exposure_multiplier(neutral["score"]) == pytest.approx(0.625)


# ---------------------------------------------------------------------------
# what the pipeline does with it
# ---------------------------------------------------------------------------

def test_cold_cache_does_not_invent_an_exposure_multiplier():
    """multiply mode: an unmeasured regime must not size the book down by 37.5%."""
    cold = regime_composite(COLD)
    res = final_score(technical=0.8, fundamental=0.4, analyst=None,
                      regime=cold["score"], s={"macro_mode": "multiply"},
                      regime_n_inputs=cold["n_inputs"])
    assert res["regime"] is None
    assert res["exposure_multiplier"] == 1.0


def test_cold_cache_leaves_the_score_unscaled():
    """The same symbol, scored with and without a resolvable macro cache, must not
    differ by a factor invented from the absence of data."""
    cold = regime_composite(COLD)
    scaled = final_score(technical=0.8, fundamental=0.4, analyst=None,
                         regime=cold["score"], s={"macro_mode": "multiply"},
                         regime_n_inputs=cold["n_inputs"])
    macro_off = final_score(technical=0.8, fundamental=0.4, analyst=None,
                            regime=None, s={"macro_mode": "off"})
    assert scaled["final"] == pytest.approx(macro_off["final"])


def test_gate_mode_reports_that_the_gate_never_ran():
    """gate mode admits the BUY either way -- but it must not claim a regime it
    never measured, or the audit trail records a macro check that did not happen."""
    cold = regime_composite(COLD)
    res = final_score(technical=0.8, fundamental=0.4, analyst=None,
                      regime=cold["score"], s={"macro_mode": "gate",
                                               "macro_gate_min": -0.5},
                      regime_n_inputs=cold["n_inputs"])
    assert res["regime"] is None


def test_gate_mode_with_a_MEASURED_regime_gates_instead_of_scaling():
    """The control for the cold-cache tests above: when the regime IS measured,
    gate mode must flatten a bullish score and must NOT apply the multiply arm's
    exposure factor. If both arms fired, 'gate' would silently be 'gate AND
    multiply' and the cold-cache assertions would be measuring the wrong thing."""
    s = {"macro_mode": "gate", "macro_gate_min": -0.5}
    bad = final_score(technical=0.8, fundamental=0.4, analyst=None,
                      regime=-0.9, s=s, regime_n_inputs=4)
    assert bad["final"] <= 0.0
    assert bad["exposure_multiplier"] == 1.0        # gate does not scale
    ok = final_score(technical=0.8, fundamental=0.4, analyst=None,
                     regime=-0.2, s=s, regime_n_inputs=4)
    assert ok["final"] > 0.0
    assert ok["exposure_multiplier"] == 1.0


def test_input_mode_drops_the_unmeasured_macro_section():
    """input mode: an unmeasured macro must be renormalized away, not averaged in
    as a 0.0 that drags every score toward flat."""
    cold = regime_composite(COLD)
    res = final_score(technical=1.0, fundamental=1.0, analyst=None,
                      regime=cold["score"],
                      s={"macro_mode": "input", "w_macro": 0.2,
                         "w_technical": 0.5, "w_fundamental": 0.3},
                      regime_n_inputs=cold["n_inputs"])
    assert res["raw"] == pytest.approx(1.0)
    assert "macro" not in res["components"]
    # ... and the count of sections behind the score must not claim the macro one.
    assert res["n_sections"] == 2


# ---------------------------------------------------------------------------
# the expert's own section builder (_build_regime uses no instance state, so it
# is callable unbound -- the cold bundle must reach final_score as None, not be
# re-flattened to 0.0 on the way)
# ---------------------------------------------------------------------------

def test_the_expert_reports_a_cold_bundle_as_unknown():
    regime = DeterministicScorer._build_regime(
        None, {"macro_inputs": {}, "index_closes": None}, {"macro_mode": "multiply"})
    assert regime["score"] is None
    assert regime["n_inputs"] == 0


def test_the_expert_still_measures_a_regime_when_the_inputs_arrive():
    """Two real inputs (index trend above its SMA200, a calm VIX) -> a measured
    regime. The unknown branch must not swallow a bundle that DID resolve."""
    closes = pd.Series([float(100 + i) for i in range(260)])
    regime = DeterministicScorer._build_regime(
        None, {"macro_inputs": {"vix": 15.0}, "index_closes": closes},
        {"macro_mode": "multiply"})
    assert regime["score"] is not None
    assert regime["n_inputs"] == 2
    assert regime["score"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# what the SYMBOL360 card shows
# ---------------------------------------------------------------------------

def test_the_card_does_not_render_a_cold_regime_as_a_number():
    cold = regime_composite(COLD)
    assert DeterministicScorer._fmt_score(cold["score"]) == "n/a"
    assert DeterministicScorer._score_signal(cold["score"]) is None


def test_the_card_still_renders_a_measured_neutral_regime_as_a_number():
    neutral = regime_composite({"vix": 1.0, "credit": -1.0},
                               {"vix": 0.5, "credit": 0.5})
    assert DeterministicScorer._fmt_score(neutral["score"]) == "+0.00"
    assert DeterministicScorer._score_signal(neutral["score"]) == "neutral"


def test_the_explanation_says_no_input_resolved():
    text, table = explain.explain_macro_section(regime_composite(COLD))
    assert "no macro input resolved" in text
