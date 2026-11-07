"""Point-in-time fixes: split-adjusted price targets (live AND backtest), the grades-historical
knowability check, and the market-cap share basis (documented, no change needed)."""
import json
import logging
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_common.core.knowability import intraday_decisions
from ba2_experts.FMPRating import FMPRating
from ba2_experts.DeterministicScorer.analyst import price_target_drift
from ba2_experts.price_target_basis import split_adjusted_target

NOW = datetime(2024, 6, 20, tzinfo=timezone.utc)   # ten days after NVDA's 10:1 split


def _row(day, published, adj):
    return {"publishedDate": day, "priceTarget": published, "adjPriceTarget": adj}


# NVDA-shaped: targets published BEFORE the split carry the unsplit figure in priceTarget.
SPLIT_HISTORY = [
    _row("2024-05-23", 1200, 120.0),
    _row("2024-05-28", 1250, 125.0),
    _row("2024-06-12", 130, 130.0),     # after the split: both fields agree
]


def test_split_adjusted_target_rule():
    assert split_adjusted_target({"priceTarget": 1200, "adjPriceTarget": 120}) == 120.0
    for bad in ({"priceTarget": 1200}, {"priceTarget": 1200, "adjPriceTarget": None},
                {"priceTarget": 1200, "adjPriceTarget": "x"}, {"adjPriceTarget": 0},
                {"adjPriceTarget": -3}):
        assert split_adjusted_target(bad) is None


def test_consensus_uses_the_split_adjusted_target():
    c = FMPRating._consensus_target_as_of(SPLIT_HISTORY, NOW, 90)
    assert c["targetConsensus"] == pytest.approx((120 + 125 + 130) / 3)
    assert c["targetHigh"] == 130.0


def test_a_row_without_adjusted_target_is_skipped_and_logged(caplog):
    rows = SPLIT_HISTORY + [{"publishedDate": "2024-06-01", "priceTarget": 9999}]
    with caplog.at_level(logging.WARNING):
        c = FMPRating._consensus_target_as_of(rows, NOW, 90)
    assert c["targetHigh"] == 130.0 and c["targetCount"] == 3
    assert any("without a usable adjPriceTarget" in r.getMessage() for r in caplog.records)
    assert FMPRating._count_targets_in_window(rows, NOW, 90) == 3


def test_deterministic_scorer_drift_uses_the_adjusted_target():
    # price 125 on today's basis; unsplit targets (~1200) would read as +860% upside.
    res = price_target_drift(SPLIT_HISTORY * 1, NOW, 125.0, window_days=90, min_targets=3)
    assert res is not None
    assert res["consensus_target"] == pytest.approx(125.0)
    assert abs(res["upside_pct"]) < 1.0


def _live_expert(consensus, history, price):
    e = FMPRating.__new__(FMPRating)
    e.id = 1
    e._gather_symbol = "NVDA"
    e._gather_window_days = 90
    e._get_current_price = lambda sym: price
    e._fetch_price_target_consensus = lambda symbol: consensus
    e._fetch_upgrade_downgrade = lambda symbol: [{"strongBuy": 1}]
    e._fetch_price_target_history = lambda symbol: history
    e._fetch_grades_historical = lambda symbol: []
    return e


def test_live_consensus_after_a_recent_split_is_split_adjusted(monkeypatch):
    """as_of=None: the vendor consensus snapshot (mixed pre/post-split targets, mean ~ 560) is
    superseded by the rebuild from the dated rows, adjusted per row, against the as-traded price."""
    mod = sys.modules[FMPRating.__module__]
    monkeypatch.setattr(mod, "replay_now", lambda as_of=None: as_of or NOW)
    mixed_snapshot = {"targetConsensus": 560.0, "targetHigh": 1250.0, "targetLow": 130.0,
                      "targetMedian": 1200.0}
    e = _live_expert(mixed_snapshot, SPLIT_HISTORY, 110.0)
    live = e._gather(LiveProviderBundle(lambda *a, **k: None), as_of=None)["consensus_data"]
    backtest = FMPRating._consensus_target_as_of(SPLIT_HISTORY, NOW, 90)
    assert live == backtest                       # one function, two callers
    assert live["targetHigh"] == 130.0 and live["targetLow"] == 120.0


def _grade(date, buy):
    return {"date": date, "analystRatingsStrongBuy": 0, "analystRatingsbuy": buy,
            "analystRatingsHold": 0, "analystRatingsSell": 0, "analystRatingsStrongSell": 0}


def test_grades_historical_respects_intraday_knowability():
    rows = [_grade("2026-06-01", 10), _grade("2026-07-01", 20)]
    as_of = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)
    daily = FMPRating._counts_as_of(rows, as_of)
    assert daily[0]["buy"] == 20                  # daily clock: unchanged
    with intraday_decisions(True):
        intraday = FMPRating._counts_as_of(rows, as_of)
    assert intraday[0]["buy"] == 10               # bare-date row public from the next session


_CACHE = os.path.expanduser("~/Documents/ba2/common/cache/fmp_history")


@pytest.mark.skipif(not os.path.isdir(_CACHE), reason="local FMP history cache absent")
@pytest.mark.parametrize("symbol,before_row_date,floor", [
    ("NVDA", "2024-01-28", 20e9),     # 10:1 on 2024-06-10; FY2024 ends before the split
    ("AAPL", "2019-09-28", 15e9),     # 4:1 on 2020-08-31; FY2019 ends before the split
])
def test_statement_share_counts_are_restated_to_todays_split_basis(symbol, before_row_date, floor):
    """Market cap = split-adjusted price x weighted_average_shares is on ONE basis because FMP
    restates historical share counts (measured: 120/120 forward splits since 2019 restated).
    Live fetches are never disk-cached, so they are restated at fetch time too."""
    path = os.path.join(_CACHE, f"income_statement_annual__{symbol}.json")
    if not os.path.exists(path):
        pytest.skip("statement not cached")
    rows = {r["date"]: r for r in json.load(open(path))}
    if before_row_date not in rows:
        pytest.skip("row not cached")
    assert rows[before_row_date]["weightedAverageShsOut"] > floor
