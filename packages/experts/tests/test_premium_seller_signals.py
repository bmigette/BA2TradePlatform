import math
from datetime import date

from ba2_experts.PremiumSeller.signals import (
    analyst_counts_score, earnings_within, grade_score, iv_rank,
    realized_vol_annualized, sma,
)


def test_iv_rank_basic():
    hist = [0.10, 0.20, 0.30, 0.40] * 6          # 24 points
    assert iv_rank(hist, 0.50) == 100.0          # all strictly below current
    assert iv_rank(hist, 0.05) == 0.0            # none below current
    assert iv_rank(hist, 0.25) == 50.0


def test_iv_rank_insufficient_history_returns_none():
    assert iv_rank([0.2] * 19, 0.3) is None      # floor: >= 20 points
    assert iv_rank([0.2] * 25, None) is None
    assert iv_rank([], 0.3) is None


def test_iv_rank_is_the_one_shared_implementation():
    """There were two "IV rank" functions in this repo computing DIFFERENT numbers:
    this one counted ``<=`` and returned an unrounded float; the account-level
    ``_iv_rank_from_series`` counts strictly ``<`` and rounds to 2dp. Two implementations
    of one statistic is exactly how the short-sign divergence happened, so this one now
    delegates. The SERIES each is fed still differs legitimately (see the docstring);
    the ARITHMETIC may not."""
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    hist = [0.10, 0.20, 0.30, 0.40] * 6
    for current in (0.05, 0.20, 0.25, 0.40, 0.50):
        assert iv_rank(hist, current) == OptionsAccountInterface._iv_rank_from_series(
            hist, current, min_samples=20)


def test_iv_rank_does_not_count_a_sample_equal_to_current():
    """``<=`` made the gate self-fulfilling: the caller appends today's IV to the history
    BEFORE ranking, so today's own sample always counted itself and every rank was
    inflated by 100/N. Strict ``<`` is what the live and backtest paths use."""
    hist = [0.30] * 24
    assert iv_rank(hist, 0.30) == 0.0, "an equal sample is not a lower one"


def test_iv_rank_rejects_an_impossible_iv():
    """Shared bound: 0.0 and NaN are broken-feed values, not the cheapest tape of the
    year. Returning a rank here would open the ``iv_rank_min`` gate at its widest."""
    hist = [0.10, 0.20, 0.30, 0.40] * 6
    assert iv_rank(hist, 0.0) is None
    assert iv_rank(hist, float("nan")) is None
    assert iv_rank([0.0] * 24, 0.30) is None, "a zero-filled history is not 24 samples"


def test_realized_vol():
    closes = [100.0] * 21                         # flat -> ~0 vol
    assert realized_vol_annualized(closes, 20) == 0.0
    assert realized_vol_annualized(closes, 20) is not None
    assert realized_vol_annualized([100.0] * 19, 20) is None   # too short


def test_realized_vol_known_value():
    # alternating +1%/-1% log returns: per-day stdev ~= 0.01, annualized ~= 0.1587
    closes, px = [100.0], 100.0
    up = math.exp(0.01)
    dn = math.exp(-0.01)
    for i in range(60):
        px *= up if i % 2 == 0 else dn
        closes.append(px)
    v = realized_vol_annualized(closes, 60)
    assert abs(v - math.sqrt(252) * 0.01) < 0.01


def test_sma():
    assert sma([1.0, 2.0, 3.0, 4.0], 4) == 2.5
    assert sma([1.0, 2.0, 3.0], 4) is None


def test_grade_score():
    assert grade_score("Strong Buy") == 5.0
    assert grade_score("Buy") == 4.0
    assert grade_score("Neutral") == 3.0
    assert grade_score("Hold") == 3.0
    assert grade_score("Sell") == 2.0
    assert grade_score("Strong Sell") == 1.0
    assert grade_score("Outperform") == 4.0
    assert grade_score("Underperform") == 2.0
    assert grade_score("some unknown shop grade") is None
    assert grade_score(None) is None


def test_analyst_counts_score():
    # One-sided rows land on the pole weights (FMP analystRatings* key spellings).
    assert analyst_counts_score({"analystRatingsStrongBuy": 10}) == 5.0
    assert analyst_counts_score({"analystRatingsbuy": 10}) == 4.0
    assert analyst_counts_score({"analystRatingsStrongSell": 4}) == 1.0
    # Mixed row -> weighted mean: (5*7 + 4*3) / 10.
    assert analyst_counts_score(
        {"analystRatingsStrongBuy": 7, "analystRatingsBuy": 3}) == 4.7
    assert analyst_counts_score(
        {"analystRatingsStrongSell": 8, "analystRatingsSell": 2}) == 1.2
    # Equal counts across all five buckets -> midpoint 3.0.
    assert analyst_counts_score(
        {"strongBuy": 1, "buy": 1, "hold": 1, "sell": 1, "strongSell": 1}) == 3.0
    # Empty / missing / non-numeric counts -> None (never a fabricated score).
    assert analyst_counts_score(None) is None
    assert analyst_counts_score({}) is None
    assert analyst_counts_score({"date": "2024-01-01"}) is None
    assert analyst_counts_score({"analystRatingsStrongBuy": 0, "analystRatingsHold": "n/a"}) is None


def test_earnings_within():
    reports = [date(2024, 5, 1), date(2024, 8, 2)]
    assert earnings_within(reports, date(2024, 4, 1), 45) is True    # May 1 inside window
    assert earnings_within(reports, date(2024, 5, 2), 45) is False   # next is Aug 2
    assert earnings_within([], date(2024, 4, 1), 45) is False
