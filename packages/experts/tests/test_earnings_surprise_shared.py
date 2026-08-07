"""Shared PEAD calculators (ba2_experts.earnings_surprise).

One formula, two consumers: FMPEarningsDrift thresholds it into a boolean entry
signal, DeterministicScorer keeps it continuous for its weighted composite. The
duplication these tests guard against is not hypothetical -- DeterministicScorer
had already reimplemented its growth calculation inline and the copy diverged
from the unit-tested original.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ba2_experts import earnings_surprise as ES

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


def _row(days_ago: int, reported: float, estimated: float) -> dict:
    d = (NOW - timedelta(days=days_ago)).date().isoformat()
    return {"report_date": d, "reported_eps": reported, "estimated_eps": estimated}


def _history(surprises, spacing=91, start=5):
    """Quarterly rows, newest first, each beating its estimate by `pct`%."""
    return [_row(start + i * spacing, 1.0 + p / 100.0, 1.0)
            for i, p in enumerate(surprises)]


# --------------------------------------------------------------------- surprise
def test_surprise_percent_keeps_the_sign_on_a_negative_consensus():
    """A 0.10 loss against an expected 0.50 loss is a BEAT. Dividing by the raw
    (negative) estimate instead of its absolute value flips that to a miss."""
    assert ES.surprise_percent(-0.10, -0.50) == pytest.approx(80.0)
    assert ES.surprise_percent(-0.90, -0.50) == pytest.approx(-80.0)
    assert ES.surprise_percent(1.10, 1.00) == pytest.approx(10.0)


def test_surprise_percent_rejects_unusable_inputs():
    assert ES.surprise_percent(1.0, 0) is None
    assert ES.surprise_percent(None, 1.0) is None
    assert ES.surprise_percent(1.0, None) is None


# ------------------------------------------------------------------- lookahead
def test_history_excludes_reports_announced_after_as_of():
    rows = [_row(-30, 2.0, 1.0), _row(10, 1.1, 1.0)]   # first one is in the FUTURE
    hist = ES.surprise_history(rows, NOW)
    assert len(hist) == 1
    assert hist[0][1] == pytest.approx(10.0)


def test_scheduled_but_unreported_quarter_is_dropped_not_scored_as_a_miss():
    """The provider coerces a missing EPS to 0, which is indistinguishable from
    a real 0.0 -- scoring it would invent a -100% surprise."""
    rows = [{"report_date": (NOW - timedelta(days=2)).date().isoformat(),
             "reported_eps": 0, "estimated_eps": 1.88},
            _row(95, 1.1, 1.0)]
    hist = ES.surprise_history(rows, NOW)
    assert len(hist) == 1, "the unreported quarter must not become a signal"
    assert hist[0][1] == pytest.approx(10.0)


def test_history_is_newest_first_regardless_of_input_order():
    rows = [_row(200, 1.05, 1.0), _row(5, 1.20, 1.0), _row(100, 1.10, 1.0)]
    hist = ES.surprise_history(rows, NOW)
    assert [round(p) for _, p in hist] == [20, 10, 5]


def test_naive_as_of_against_dated_rows_does_not_raise():
    assert ES.surprise_history([_row(10, 1.1, 1.0)], datetime(2026, 6, 13)) != []


# ------------------------------------------------------------------------- SUE
def test_sue_standardizes_by_the_names_own_dispersion():
    """The same 6% beat is a big deal for a steady reporter and noise for a
    volatile one -- that difference is the whole point of standardizing."""
    steady = ES.standardized_surprise(_history([6, 1, 0, 1, 0, 1]), NOW)
    volatile = ES.standardized_surprise(_history([6, 40, -35, 30, -28, 25]), NOW)
    assert steady > volatile


def test_sue_is_none_without_enough_history():
    assert ES.standardized_surprise(_history([10, 5]), NOW) is None


# ----------------------------------------------------------------- pead_score
def test_pead_score_is_none_when_nothing_to_score():
    assert ES.pead_score([], NOW) is None
    assert ES.pead_score(None, NOW) is None


def test_pead_score_none_once_the_drift_window_is_spent():
    """None, not 0.0: 'no signal' and 'reported exactly in line' are different,
    and a composite that renormalizes over available sections needs to tell them
    apart."""
    assert ES.pead_score([_row(400, 1.20, 1.0)], NOW) is None
    fresh = ES.pead_score([_row(3, 1.20, 1.0)], NOW)
    assert fresh is not None and fresh["score"] > 0


def test_pead_score_decays_with_time_since_the_report():
    s = {"earnings_use_sue": False}
    day3 = ES.pead_score([_row(3, 1.20, 1.0)], NOW, s)["score"]
    day30 = ES.pead_score([_row(30, 1.20, 1.0)], NOW, s)["score"]
    day60 = ES.pead_score([_row(60, 1.20, 1.0)], NOW, s)["score"]
    assert day3 > day30 > day60 > 0


def test_pead_score_signs_and_bounds():
    s = {"earnings_use_sue": False}
    beat = ES.pead_score([_row(2, 1.50, 1.0)], NOW, s)
    miss = ES.pead_score([_row(2, 0.50, 1.0)], NOW, s)
    assert 0 < beat["score"] <= 1.0
    assert -1.0 <= miss["score"] < 0
    assert beat["basis"] == "surprise_pct"


def test_pead_score_prefers_sue_when_history_allows():
    out = ES.pead_score(_history([8, 1, 0, 1, 0, 1]), NOW)
    assert out["basis"] == "sue" and out["sue"] is not None


# ------------------------------------------------- the two consumers agree
def test_earnings_drift_expert_uses_the_shared_formula():
    from ba2_experts.FMPEarningsDrift import evaluate_earnings_drift

    row = {"report_date": (NOW - timedelta(days=2)).date().isoformat(),
           "reported_eps": -0.10, "estimated_eps": -0.50}
    out = evaluate_earnings_drift(row, NOW, surprise_min_pct=5.0,
                                  max_days_since_report=10)
    assert out["surprise_pct"] == pytest.approx(ES.surprise_percent(-0.10, -0.50), abs=0.01)
    assert out["is_signal"] is True, "a loss narrower than feared is a beat"


def test_scorer_section_is_wired_and_renormalizes_when_absent():
    from ba2_experts.DeterministicScorer import combine as C

    off = C.final_score(technical=0.8, fundamental=None, analyst=None, regime=None,
                        s={}, earnings=0.9)
    on = C.final_score(technical=0.8, fundamental=None, analyst=None, regime=None,
                       s={"w_earnings": 0.4}, earnings=0.9)
    assert off["final"] == pytest.approx(
        C.final_score(0.8, None, None, None, {})["final"]), \
        "w_earnings defaults to 0, so the section must not move anything yet"
    assert on["final"] > off["final"], "with weight, a strong beat must lift the score"
    assert "earnings" in on["components"]


# ------------------------------------------------- ANALYST: price-target leg
def _target(days_ago: int, target: float) -> dict:
    d = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    return {"publishedDate": d, "priceTarget": target}


def test_price_target_drift_needs_real_coverage():
    """A 'consensus' of one or two analysts is the degenerate thin-coverage trap
    FMPRating already guards; the leg must drop out rather than fabricate one."""
    from ba2_experts.DeterministicScorer.analyst import price_target_drift

    assert price_target_drift([_target(5, 200.0)], NOW, 100.0) is None
    assert price_target_drift([_target(5, 200.0), _target(9, 190.0)], NOW, 100.0) is None
    ok = price_target_drift([_target(5, 200.0), _target(9, 190.0), _target(20, 180.0)],
                            NOW, 100.0)
    assert ok is not None and ok["n_targets"] == 3


def test_price_target_drift_excludes_targets_published_after_as_of():
    from ba2_experts.DeterministicScorer.analyst import price_target_drift

    rows = [_target(-5, 500.0)] + [_target(10 + i, 120.0) for i in range(3)]
    out = price_target_drift(rows, NOW, 100.0)
    assert out["n_targets"] == 3, "a target published after as_of is lookahead"
    assert out["consensus_target"] == pytest.approx(120.0)


def test_price_target_drift_separates_upside_from_revision_direction():
    """A high but SINKING target is not the same signal as one being marked up."""
    from ba2_experts.DeterministicScorer.analyst import price_target_drift

    rising = price_target_drift(
        [_target(80, 100.0), _target(70, 105.0), _target(10, 140.0), _target(5, 150.0)],
        NOW, 100.0)
    falling = price_target_drift(
        [_target(80, 150.0), _target(70, 140.0), _target(10, 105.0), _target(5, 100.0)],
        NOW, 100.0)
    assert rising["drift_score"] > 0 > falling["drift_score"]
    assert rising["score"] > falling["score"]


def test_analyst_section_survives_on_whichever_leg_exists():
    from ba2_experts.DeterministicScorer.analyst import analyst_section_score

    targets = [_target(10 + i, 130.0) for i in range(4)]
    only_targets = analyst_section_score(None, targets, NOW, 100.0)
    assert only_targets is not None and set(only_targets["legs"]) == {"targets"}
    assert analyst_section_score(None, None, NOW, 100.0) is None, \
        "no legs must mean 'no section', not a fabricated neutral 0"
