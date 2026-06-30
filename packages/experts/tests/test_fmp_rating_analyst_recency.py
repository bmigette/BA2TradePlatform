"""FMPRating rating-recency filter (``max_analyst_age_months``).

FMP's aggregate analyst count (upgrades-downgrades-consensus / grades-historical) is undated /
a monthly snapshot and can be inflated by long-stale ratings (ASC: 17 standing vs ~2-4 active).
When max_analyst_age_months > 0, min_analysts instead counts DISTINCT analysts (gradingCompany)
who issued/affirmed a rating within that window, from FMP's dated individual `grades` endpoint,
as-of the analysis date (no lookahead). The recency filter kicks BEFORE the min_analysts gate, so
"6mo + min_analysts 10" means 10 analysts active within 6 months. 0 = filter OFF (bucket count).
"""
from datetime import datetime, timezone

from ba2_experts.FMPRating import FMPRating
from ba2_common.core.types import OrderRecommendation

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)

# Healthy consensus (clears the price-target guard when min_price_targets_per_quarter=0 anyway).
CONSENSUS = {"targetConsensus": 130.0, "targetHigh": 160.0, "targetLow": 110.0,
             "targetMedian": 128.0, "targetCount": 5}
# Aggregate buckets sum to 20 — a deliberately INFLATED standing count, so a test proves the
# recency path ignores it and counts the dated grades instead.
UPGRADE = [{"strongBuy": 10, "buy": 5, "hold": 3, "sell": 1, "strongSell": 1}]

# Dated individual grades: 3 DISTINCT companies active within ~6 months of NOW (A,B,C — B twice),
# plus one stale (2024) and one FUTURE (must be ignored: no-lookahead).
GRADES = [
    {"date": "2026-06-01", "gradingCompany": "Alpha", "newGrade": "Buy", "action": "maintain"},
    {"date": "2026-05-15", "gradingCompany": "Beta", "newGrade": "Buy", "action": "maintain"},
    {"date": "2026-04-01", "gradingCompany": "Beta", "newGrade": "Buy", "action": "maintain"},
    {"date": "2026-03-20", "gradingCompany": "Gamma", "newGrade": "Hold", "action": "downgrade"},
    {"date": "2024-01-10", "gradingCompany": "Delta", "newGrade": "Buy", "action": "maintain"},  # stale
    {"date": "2026-12-31", "gradingCompany": "Omega", "newGrade": "Buy", "action": "maintain"},  # future
]

BASE = {"profit_ratio": 1.0, "min_analysts": 3, "target_price_type": "consensus",
        "price_target_window_days": 90, "min_price_targets_per_quarter": 0}


def _bundle(grades=GRADES, upgrade=UPGRADE, consensus=CONSENSUS, price=100.0):
    return {"consensus_data": consensus, "upgrade_data": upgrade,
            "current_price": price, "symbol": "ASC", "analyst_grades": grades}


def _expert():
    return FMPRating.__new__(FMPRating)


# --------------------------------------------------------------------------- #
# _count_recent_analysts
# --------------------------------------------------------------------------- #
def test_count_recent_distinct_in_window():
    # 6 months (~180d) back from NOW -> Alpha, Beta (x2 -> 1), Gamma => 3 distinct.
    assert FMPRating._count_recent_analysts(GRADES, NOW, 6) == 3


def test_count_recent_no_lookahead_and_stale_excluded():
    # The 2026-12-31 future row and the 2024 stale row never count.
    assert FMPRating._count_recent_analysts(GRADES, NOW, 3) == 3   # 3mo (~90d): Alpha, Beta, Gamma
    # a 1-month window (~30d, floor 2026-05-14) keeps Alpha (06-01) + Beta (05-15) -> 2;
    # Gamma (03-20) drops out, and the stale/future rows never count.
    assert FMPRating._count_recent_analysts(GRADES, NOW, 1) == 2


def test_count_recent_zero_or_empty():
    assert FMPRating._count_recent_analysts(GRADES, NOW, 0) == 0
    assert FMPRating._count_recent_analysts([], NOW, 6) == 0
    assert FMPRating._count_recent_analysts(None, NOW, 6) == 0


# --------------------------------------------------------------------------- #
# _process gate
# --------------------------------------------------------------------------- #
def test_process_recency_uses_grades_not_buckets():
    """max_age>0: the gate counts the 3 recent distinct analysts, NOT the inflated 20-bucket
    sum. With min_analysts=5 the recency count (3) is short -> SKIP (proves buckets are ignored)."""
    settings = dict(BASE, min_analysts=5, max_analyst_age_months=6)
    rec = _expert()._process(_bundle(), settings, as_of=NOW)
    assert rec.skip is True
    assert rec.skip_reason == "insufficient analysts"
    assert "3 active within 6mo" in rec.details


def test_process_recency_passes_when_enough_recent():
    """3 recent analysts clear min_analysts=3 under the recency filter (not skipped for count)."""
    settings = dict(BASE, min_analysts=3, max_analyst_age_months=6)
    rec = _expert()._process(_bundle(), settings, as_of=NOW)
    assert not (rec.skip and rec.skip_reason == "insufficient analysts")
    assert rec.signal == OrderRecommendation.BUY


def test_process_zero_age_falls_back_to_bucket_count():
    """max_age=0: the gate uses the 20-bucket sum (recency OFF), so min_analysts=15 passes even
    though only 3 analysts are recent — proving 0 disables the filter."""
    settings = dict(BASE, min_analysts=15, max_analyst_age_months=0)
    rec = _expert()._process(_bundle(), settings, as_of=NOW)
    assert not (rec.skip and rec.skip_reason == "insufficient analysts")


def test_process_recency_skips_when_no_grades():
    """Filter on but no grades available -> count 0 -> SKIP (fail-closed)."""
    settings = dict(BASE, min_analysts=3, max_analyst_age_months=6)
    rec = _expert()._process(_bundle(grades=None), settings, as_of=NOW)
    assert rec.skip is True
    assert rec.skip_reason == "insufficient analysts"
    assert "0 active within 6mo" in rec.details


# --------------------------------------------------------------------------- #
# _format_analyst_details_md (LIVE-only UI detail; best-effort)
# --------------------------------------------------------------------------- #
import logging


def _detail_expert(grades, targets):
    e = FMPRating.__new__(FMPRating)
    e.logger = logging.getLogger("t")
    e._fetch_analyst_grades = lambda s: grades
    e._fetch_price_target_history = lambda s: targets
    return e


def test_format_analyst_details_md_builds_both_tables():
    targets = [{"publishedDate": "2026-06-01", "analystCompany": "Evercore",
                "priceTarget": 120.0, "priceWhenPosted": 100.0}]
    md = _detail_expert(GRADES, targets)._format_analyst_details_md("ASC")
    assert "Recent Analyst Ratings" in md and "Recent Price Targets" in md
    assert "Alpha" in md and "Evercore" in md and "$120.00" in md


def test_format_analyst_details_md_none_when_empty():
    assert _detail_expert([], [])._format_analyst_details_md("ASC") is None


def test_format_analyst_details_md_swallows_fetch_errors():
    def boom(_s):
        raise RuntimeError("fetch failed")
    e = _detail_expert(None, [])
    e._fetch_analyst_grades = boom
    assert e._format_analyst_details_md("ASC") is None   # best-effort, never raises
