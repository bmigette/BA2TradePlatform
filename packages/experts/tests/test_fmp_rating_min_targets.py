"""FMPRating degenerate-consensus guard (``min_price_targets_per_quarter``).

FMP windows its price-target CONSENSUS to ~the last quarter, so a name with a
single recent analyst yields a DEGENERATE consensus (high==low==median, e.g. the
ASC $19 case). ``min_analysts`` does NOT catch this because it counts RATINGS
(Strong Buy..Strong Sell), a much larger pool than the analysts who set a recent
price target. This guard requires at least N price targets BEHIND the consensus
before acting; 0 disables it (the value the grid explores as "no check").

The targets-behind-the-consensus count is surfaced as ``targetCount`` on the
consensus dict: live counts the trailing-quarter price-target history; backtest
(``_consensus_target_as_of``) counts the targets over the reconstruction window.
"""
from datetime import datetime, timezone

from ba2_experts.FMPRating import FMPRating
from ba2_common.core.types import OrderRecommendation

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)

# A degenerate single-analyst consensus (the ASC shape: high==low==median).
DEGENERATE = {"targetConsensus": 19.0, "targetHigh": 19.0, "targetLow": 19.0,
              "targetMedian": 19.0, "targetCount": 1}
# A healthy multi-analyst consensus with upside above current (100).
HEALTHY = {"targetConsensus": 130.0, "targetHigh": 160.0, "targetLow": 110.0,
           "targetMedian": 128.0, "targetCount": 5}
UPGRADE = [{"strongBuy": 10, "buy": 5, "hold": 3, "sell": 1, "strongSell": 1}]  # 20 ratings

# settings WITHOUT the new key => guard disabled (back-compat); _process reads it
# via settings.get(..., 0) so older configs behave exactly as before.
BASE = {"profit_ratio": 1.0, "min_analysts": 10, "target_price_type": "consensus",
        "price_target_window_days": 90}


def _bundle(consensus, upgrade=UPGRADE, price=100.0):
    return {"consensus_data": consensus, "upgrade_data": upgrade,
            "current_price": price, "symbol": "ASC"}


def _expert():
    return FMPRating.__new__(FMPRating)


# --------------------------------------------------------------------------- #
# _count_targets_in_window helper
# --------------------------------------------------------------------------- #
def test_count_targets_in_window_filters_window_and_nulls():
    history = [
        {"publishedDate": "2026-06-10", "priceTarget": 110.0},   # in window
        {"publishedDate": "2026-06-01", "priceTarget": 150.0},   # in window
        {"publishedDate": "2026-01-01", "priceTarget": 140.0},   # > 90d before -> out
        {"publishedDate": "2026-12-31", "priceTarget": 99.0},    # future -> out
        {"publishedDate": "2026-06-05", "priceTarget": None},    # null target -> out
    ]
    assert FMPRating._count_targets_in_window(history, NOW, 90) == 2
    assert FMPRating._count_targets_in_window([], NOW, 90) == 0
    assert FMPRating._count_targets_in_window(None, NOW, 90) == 0


# --------------------------------------------------------------------------- #
# backtest reconstruction surfaces targetCount
# --------------------------------------------------------------------------- #
def test_consensus_target_as_of_surfaces_count():
    history = [
        {"publishedDate": "2026-06-10", "priceTarget": 110.0},
        {"publishedDate": "2026-06-05", "priceTarget": 150.0},
        {"publishedDate": "2026-06-01", "priceTarget": 130.0},
    ]
    c = FMPRating._consensus_target_as_of(history, NOW, 90)
    assert c["targetCount"] == 3
    # single recent analyst -> degenerate consensus, targetCount 1
    one = FMPRating._consensus_target_as_of(
        [{"publishedDate": "2026-06-10", "priceTarget": 19.0}], NOW, 90)
    assert one["targetCount"] == 1
    assert one["targetHigh"] == one["targetLow"] == 19.0


# --------------------------------------------------------------------------- #
# _process guard
# --------------------------------------------------------------------------- #
def test_process_skips_thin_consensus():
    """targetCount (1) below min_price_targets_per_quarter (3) => SKIP."""
    settings = dict(BASE, min_price_targets_per_quarter=3)
    rec = _expert()._process(_bundle(DEGENERATE), settings)
    assert rec.skip is True
    assert rec.skip_reason == "insufficient price targets"
    assert rec.signal == OrderRecommendation.HOLD
    assert "1 < 3" in rec.details


def test_process_no_check_when_zero():
    """min_price_targets_per_quarter=0 disables the guard: the SAME degenerate
    consensus is NOT skipped for this reason (it proceeds to the normal math)."""
    settings = dict(BASE, min_price_targets_per_quarter=0)
    rec = _expert()._process(_bundle(DEGENERATE), settings)
    assert rec.skip_reason != "insufficient price targets"


def test_process_missing_key_disables_guard():
    """A settings dict without the key (legacy config) leaves the guard OFF."""
    rec = _expert()._process(_bundle(DEGENERATE), BASE)
    assert rec.skip_reason != "insufficient price targets"


def test_process_passes_when_enough_targets():
    """A healthy 5-target consensus clears the min-3 guard (skip is for thin only)."""
    settings = dict(BASE, min_price_targets_per_quarter=3)
    rec = _expert()._process(_bundle(HEALTHY), settings)
    assert rec.skip_reason != "insufficient price targets"
    assert rec.signal == OrderRecommendation.BUY


def test_process_skips_when_count_unknown_and_guard_on():
    """Guard on but consensus carries no targetCount => treat as unknown -> SKIP
    (fail-closed: never act on a consensus whose target depth can't be verified)."""
    settings = dict(BASE, min_price_targets_per_quarter=3)
    no_count = {k: v for k, v in HEALTHY.items() if k != "targetCount"}
    rec = _expert()._process(_bundle(no_count), settings)
    assert rec.skip is True
    assert rec.skip_reason == "insufficient price targets"
    assert "unknown < 3" in rec.details
