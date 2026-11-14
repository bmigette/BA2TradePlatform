"""DeterministicScorer grades leg reads FMP grades-historical rows by their REAL field names.

FMP ``stable/grades-historical`` rows (cache AND live, measured 2026-11-14: all 320,308 cached rows
over 5,086 symbols, and every live DS capture on 2026-11-13) carry ``analystRatingsStrongBuy`` /
``analystRatingsBuy`` / ``analystRatingsHold`` / ``analystRatingsSell`` /
``analystRatingsStrongSell``. DS's bucket reader looked for ``strongBuy``/``buy``/... only, so
every row summed to zero, every row was skipped, and the grades leg was None on 100% of bars --
backtest and live alike. FMPRating already mapped these names; both experts now read a row
through ONE shared helper (``ba2_experts.analyst_grades.grade_bucket_counts``).
"""
from datetime import datetime, timezone

import pytest

from ba2_experts.DeterministicScorer import analyst as A

# Verbatim AAPL rows from the FMP grades-historical cache (monthly snapshots dated the 1st).
_AAPL_ROWS = [
    {"symbol": "AAPL", "date": "2024-06-01", "analystRatingsStrongBuy": 12, "analystRatingsBuy": 24,
     "analystRatingsHold": 12, "analystRatingsSell": 1, "analystRatingsStrongSell": 0},
    {"symbol": "AAPL", "date": "2024-05-01", "analystRatingsStrongBuy": 10, "analystRatingsBuy": 24,
     "analystRatingsHold": 7, "analystRatingsSell": 1, "analystRatingsStrongSell": 0},
    {"symbol": "AAPL", "date": "2024-04-01", "analystRatingsStrongBuy": 10, "analystRatingsBuy": 24,
     "analystRatingsHold": 7, "analystRatingsSell": 1, "analystRatingsStrongSell": 0},
]
_AS_OF = datetime(2024, 6, 15, tzinfo=timezone.utc)


def test_revision_momentum_scores_real_fmp_rows():
    out = A.revision_momentum([dict(r) for r in _AAPL_ROWS], _AS_OF, window_days=90,
                              halflife_days=30)
    assert out is not None, "real analystRatings* rows must produce a grades score"
    assert out["n_rows"] == 3
    # 34-36 buy-side vs 1 sell-side per row: strongly positive net
    assert out["net_revision"] > 0.6
    assert out["weighted_downgrades"] > 0.0  # the Sell bucket is read too


def test_analyst_section_has_a_grades_leg_on_real_rows():
    sec = A.analyst_section_score([dict(r) for r in _AAPL_ROWS], None, _AS_OF, 190.0, {})
    assert sec is not None and "grades" in sec["legs"]


@pytest.mark.parametrize("buy_key", ["analystRatingsBuy", "analystRatingsbuy", "buy"])
def test_shared_helper_maps_every_known_spelling(buy_key):
    from ba2_experts import analyst_grades as G
    row = {"date": "2024-06-01", "analystRatingsStrongBuy": 2, buy_key: 3,
           "analystRatingsHold": 4, "analystRatingsSell": 5, "analystRatingsStrongSell": 6}
    assert G.grade_bucket_counts(row) == {"strongBuy": 2, "buy": 3, "hold": 4, "sell": 5,
                                          "strongSell": 6}


def test_shared_helper_short_names():
    from ba2_experts import analyst_grades as G
    row = {"strongBuy": 1, "buy": 2, "hold": 3, "sell": 4, "strongSell": 5}
    assert G.grade_bucket_counts(row) == {"strongBuy": 1, "buy": 2, "hold": 3, "sell": 4,
                                          "strongSell": 5}


def test_shared_helper_refuses_an_unrecognised_row():
    """No bucket field at all is a schema change, not 'zero analysts': None, never zeros."""
    from ba2_experts import analyst_grades as G
    assert G.grade_bucket_counts({"date": "2024-06-01", "ratingStrongBuy": 3}) is None


def test_fmprating_and_ds_use_the_one_helper():
    """One alias TABLE, two callers: FMPRating's as-of reconstruction and DS's revision momentum."""
    import importlib
    from ba2_experts import analyst_grades as G
    FR = importlib.import_module("ba2_experts.FMPRating")
    assert FR.FMPRating._GRADES_FIELD_ALIASES is G.GRADES_FIELD_ALIASES
    assert A.grade_bucket_counts is G.grade_bucket_counts
    counts = FR.FMPRating._counts_as_of([dict(r) for r in _AAPL_ROWS], _AS_OF)
    assert counts == [G.grade_bucket_counts(_AAPL_ROWS[0])]


def test_fmprating_partial_row_reading_is_unchanged():
    """FMPRating keeps its pre-fix reading (a missing bucket counts 0) so stored results do not move;
    DS refuses the same row instead of reading zeros."""
    import importlib
    from ba2_experts import analyst_grades as G
    FR = importlib.import_module("ba2_experts.FMPRating")
    row = {"date": _AAPL_ROWS[0]["date"], "analystRatingsStrongBuy": 5, "analystRatingsBuy": 3}
    assert FR.FMPRating._counts_as_of([row], _AS_OF) == [
        {"strongBuy": 5, "buy": 3, "hold": 0, "sell": 0, "strongSell": 0}]
    assert G.grade_bucket_counts(row) is None
    both = dict(row, analystRatingsbuy=9, analystRatingsHold=1, analystRatingsSell=0, analystRatingsStrongSell=0)
    assert G.grade_bucket_counts(both)["buy"] == 9        # the pre-fix alias order: ...buy first
