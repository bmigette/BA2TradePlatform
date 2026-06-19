"""FMPRating BACKTEST reconstruction — no-lookahead proof.

FMP exposes only the CURRENT consensus (price-target-consensus,
upgrades-downgrades-consensus; n=1, no per-row date). The LIVE (as_of=None) path
keeps using those snapshots. The BACKTEST (as_of set) path instead reconstructs the
SAME two inputs as-of each date from FMP's dated history and feeds FMPRating's
EXISTING pure math (_calculate_recommendation) verbatim:

  * buy/hold/sell counts  <- grades-historical (latest dated row whose date <= as_of)
  * consensus price target <- rolling average of v4/price-target individual analyst
    rows whose publishedDate <= as_of within a trailing window (price_target_window_days)

These tests prove, with fixtures and NO live FMP key / network:
  1. a PAST as_of reproduces the consensus math from the dated fixtures, and
  2. it is NO-LOOKAHEAD: rows dated/published AFTER as_of are ignored, and rows
     older than the trailing window are excluded; only rows <= as_of within the
     window drive the reconstruction (and therefore the decision).
"""
from datetime import datetime, timezone

import pandas as pd

from ba2_experts.FMPRating import FMPRating
from ba2_common.core.types import OrderRecommendation
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle


# Two distinct as_of dates with DIFFERENT visible history -> DIFFERENT decisions.
AS_OF_EARLY = datetime(2026, 3, 15, tzinfo=timezone.utc)
AS_OF_LATE = datetime(2026, 6, 13, tzinfo=timezone.utc)

SETTINGS = {"profit_ratio": 1.0, "min_analysts": 10, "target_price_type": "consensus",
            "price_target_window_days": 90}


# --------------------------------------------------------------------------- #
# Dated FMP history. Each row carries a date/publishedDate so the as_of filter
# can be exercised. The LATE window (90d back from 2026-06-13) sees only the
# 2026-04..06 rows; the EARLY window (90d back from 2026-03-15) sees only the
# 2025-12..2026-03 rows. The FAR-FUTURE rows (2026-12) must NEVER be used.
# --------------------------------------------------------------------------- #

# grades-historical: BUY-dominant for the LATE as_of, SELL-dominant for the EARLY
# as_of, and a poisoned FUTURE row that (if it leaked) would flip the LATE decision.
GRADES_HISTORY = [
    # EARLY-visible (date <= 2026-03-15): sell-dominant.
    {"date": "2026-02-01", "analystRatingsStrongBuy": 0, "analystRatingsbuy": 1,
     "analystRatingsHold": 2, "analystRatingsSell": 8, "analystRatingsStrongSell": 4},
    {"date": "2026-03-01", "analystRatingsStrongBuy": 0, "analystRatingsbuy": 2,
     "analystRatingsHold": 3, "analystRatingsSell": 7, "analystRatingsStrongSell": 3},
    # LATE-visible (date <= 2026-06-13): buy-dominant.
    {"date": "2026-05-20", "analystRatingsStrongBuy": 12, "analystRatingsbuy": 6,
     "analystRatingsHold": 2, "analystRatingsSell": 1, "analystRatingsStrongSell": 0},
    # FUTURE (after every as_of here): MUST be ignored (no-lookahead). If leaked it
    # would dominate as the latest row and force a strong-SELL.
    {"date": "2026-12-01", "analystRatingsStrongBuy": 0, "analystRatingsbuy": 0,
     "analystRatingsHold": 0, "analystRatingsSell": 50, "analystRatingsStrongSell": 50},
]

# v4/price-target individual analyst notes.
PRICE_TARGET_HISTORY = [
    # EARLY window (publishedDate in ~2026-01..03): below current (100) -> bearish.
    {"publishedDate": "2026-01-10", "priceTarget": 80.0},
    {"publishedDate": "2026-02-15", "priceTarget": 90.0},
    {"publishedDate": "2026-03-05", "priceTarget": 85.0},
    # LATE window (publishedDate in ~2026-04..06): above current (100) -> bullish.
    {"publishedDate": "2026-05-01", "priceTarget": 130.0},
    {"publishedDate": "2026-05-15", "priceTarget": 150.0},
    {"publishedDate": "2026-06-05", "priceTarget": 140.0},
    # FUTURE (after every as_of): MUST be ignored (no-lookahead). If leaked it would
    # blow up the average.
    {"publishedDate": "2026-12-20", "priceTarget": 9999.0},
]


class _FakeOHLCV:
    """Constant close=100 so current_price is pinned for both as_of dates."""
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [100.0]})


def _resolver():
    ohlcv = _FakeOHLCV()
    return lambda cat, name, **kw: ohlcv


def _expert(price_target_history=PRICE_TARGET_HISTORY, grades_history=GRADES_HISTORY):
    e = FMPRating.__new__(FMPRating)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._fetch_price_target_history = lambda symbol: price_target_history
    e._fetch_grades_historical = lambda symbol: grades_history
    # The live snapshot fetchers MUST NOT be called on the as_of path; make them loud.
    def _boom(symbol):
        raise AssertionError("backtest (as_of) path must not call a live snapshot fetcher")
    e._fetch_price_target_consensus = _boom
    e._fetch_upgrade_downgrade = _boom
    return e


def _ctx(as_of):
    return BacktestContext(providers=LiveProviderBundle(_resolver()),
                           settings=SETTINGS, as_of=as_of, extra={"symbol": "AAPL"})


# --------------------------------------------------------------------------- #
# Pure reconstructor unit tests (the no-lookahead math, fixtures only).
# --------------------------------------------------------------------------- #
def test_counts_as_of_picks_latest_row_on_or_before_as_of():
    # LATE: latest row <= 2026-06-13 is the 2026-05-20 buy-dominant row.
    counts = FMPRating._counts_as_of(GRADES_HISTORY, AS_OF_LATE)
    assert counts == [{"strongBuy": 12, "buy": 6, "hold": 2, "sell": 1, "strongSell": 0}]
    # EARLY: latest row <= 2026-03-15 is the 2026-03-01 sell-dominant row.
    counts_early = FMPRating._counts_as_of(GRADES_HISTORY, AS_OF_EARLY)
    assert counts_early == [{"strongBuy": 0, "buy": 2, "hold": 3, "sell": 7, "strongSell": 3}]


def test_counts_as_of_ignores_future_rows():
    # The 2026-12-01 row (50 sell / 50 strongSell) must NEVER be selected.
    counts = FMPRating._counts_as_of(GRADES_HISTORY, AS_OF_LATE)
    assert counts[0]["strongSell"] == 0  # would be 50 if the future row leaked
    # Removing the future row gives the IDENTICAL result -> it was unused.
    no_future = [r for r in GRADES_HISTORY if r["date"] != "2026-12-01"]
    assert FMPRating._counts_as_of(no_future, AS_OF_LATE) == counts


def test_counts_as_of_no_coverage_returns_none():
    before_any = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert FMPRating._counts_as_of(GRADES_HISTORY, before_any) is None


def test_consensus_target_as_of_rolling_window_average():
    # LATE window (90d back from 2026-06-13 -> floor 2026-03-15): only the
    # 130/150/140 rows qualify -> mean 140, max 150, min 130, median 140.
    c = FMPRating._consensus_target_as_of(PRICE_TARGET_HISTORY, AS_OF_LATE, 90)
    assert c == {"targetConsensus": 140.0, "targetHigh": 150.0,
                 "targetLow": 130.0, "targetMedian": 140.0}
    # EARLY window (90d back from 2026-03-15 -> floor 2025-12-15): only the
    # 80/90/85 rows qualify -> mean 85, max 90, min 80, median 85.
    c_early = FMPRating._consensus_target_as_of(PRICE_TARGET_HISTORY, AS_OF_EARLY, 90)
    assert c_early == {"targetConsensus": 85.0, "targetHigh": 90.0,
                       "targetLow": 80.0, "targetMedian": 85.0}


def test_consensus_target_as_of_ignores_future_and_pre_window_rows():
    # Future 9999 row must be ignored; pre-window EARLY rows must be excluded from
    # the LATE window. Dropping them yields the IDENTICAL result.
    c = FMPRating._consensus_target_as_of(PRICE_TARGET_HISTORY, AS_OF_LATE, 90)
    in_window_only = [
        {"publishedDate": "2026-05-01", "priceTarget": 130.0},
        {"publishedDate": "2026-05-15", "priceTarget": 150.0},
        {"publishedDate": "2026-06-05", "priceTarget": 140.0},
    ]
    assert FMPRating._consensus_target_as_of(in_window_only, AS_OF_LATE, 90) == c
    assert c["targetHigh"] == 150.0  # would be 9999 if the future row leaked


def test_consensus_target_window_setting_changes_inclusion():
    # A WIDER window (250d) at the EARLY as_of pulls in the 2026-01..03 rows AND
    # the 2025-12 floor — proving the window is honored (vs the 90d default).
    wide = FMPRating._consensus_target_as_of(PRICE_TARGET_HISTORY, AS_OF_EARLY, 250)
    # 250d back from 2026-03-15 -> floor ~2025-07-08: still only 80/90/85 here, but
    # a 1-day window excludes everything -> None (no targets that recent).
    narrow = FMPRating._consensus_target_as_of(PRICE_TARGET_HISTORY, AS_OF_EARLY, 1)
    assert wide is not None
    assert narrow is None


# --------------------------------------------------------------------------- #
# End-to-end: analyze_as_of reproduces the consensus math from the dated history.
# --------------------------------------------------------------------------- #
def test_analyze_as_of_late_reproduces_buy_from_dated_history():
    """LATE as_of: visible counts are buy-dominant and targets are above current
    -> BUY, with the EXACT confidence/profit _calculate_recommendation produces on
    the reconstructed inputs."""
    e = _expert()
    rec = e.analyze_as_of(AS_OF_LATE, _ctx(AS_OF_LATE))

    # Reconstructed inputs (no-lookahead) fed to the EXISTING pure math.
    upgrade = FMPRating._counts_as_of(GRADES_HISTORY, AS_OF_LATE)
    consensus = FMPRating._consensus_target_as_of(PRICE_TARGET_HISTORY, AS_OF_LATE, 90)
    direct = e._calculate_recommendation(consensus, upgrade, 100.0, 1.0, 10, "consensus")

    assert rec.signal == OrderRecommendation.BUY
    assert rec.skip is False
    assert rec.signal == direct["signal"]
    assert rec.confidence == round(direct["confidence"], 1)
    assert rec.expected_profit_percent == direct["expected_profit_percent"]
    assert rec.details == direct["details"]
    assert rec.expected_profit_percent > 0.0  # upside to 140 consensus


def test_analyze_as_of_early_reproduces_sell_from_dated_history():
    """EARLY as_of: visible counts are sell-dominant and targets are below current
    -> SELL. Distinct from the LATE BUY, proving the as_of date drives the decision
    via the reconstructed (not snapshot) inputs."""
    e = _expert()
    rec = e.analyze_as_of(AS_OF_EARLY, _ctx(AS_OF_EARLY))
    assert rec.signal == OrderRecommendation.SELL
    assert rec.skip is False


def test_no_lookahead_future_rows_do_not_change_decision():
    """Dropping every row dated after the as_of yields the IDENTICAL recommendation
    -> the future rows were never used (no-lookahead)."""
    e_full = _expert()
    rec_full = e_full.analyze_as_of(AS_OF_LATE, _ctx(AS_OF_LATE))

    grades_no_future = [r for r in GRADES_HISTORY if r["date"] != "2026-12-01"]
    pt_no_future = [r for r in PRICE_TARGET_HISTORY if r["publishedDate"] != "2026-12-20"]
    e_trim = _expert(price_target_history=pt_no_future, grades_history=grades_no_future)
    rec_trim = e_trim.analyze_as_of(AS_OF_LATE, _ctx(AS_OF_LATE))

    assert rec_full.signal == rec_trim.signal
    assert rec_full.confidence == rec_trim.confidence
    assert rec_full.expected_profit_percent == rec_trim.expected_profit_percent
    assert rec_full.details == rec_trim.details


def test_as_of_path_does_not_call_live_snapshot_fetchers():
    """The backtest path must reconstruct from dated history, NOT hit the live
    current-snapshot endpoints (the stubs raise if called)."""
    e = _expert()
    # Would raise AssertionError inside _gather if a snapshot fetcher were called.
    rec = e.analyze_as_of(AS_OF_LATE, _ctx(AS_OF_LATE))
    assert rec.signal == OrderRecommendation.BUY


def test_no_coverage_as_of_skips():
    """Before any dated history exists -> no reconstructed consensus -> SKIP
    (skip_reason='no consensus data'), mirroring the live no-coverage skip."""
    e = _expert()
    before_any = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rec = e.analyze_as_of(before_any, _ctx(before_any))
    assert rec.skip is True
    assert rec.skip_reason == "no consensus data"
    assert rec.signal == OrderRecommendation.HOLD
