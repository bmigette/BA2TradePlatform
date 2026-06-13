"""Tests for Weinstein stage classification (screener Stage 2 filter)."""

import pytest

from ba2_trade_platform.core.weinstein import classify_weinstein_stage, is_stage2


def _series(n, start, step):
    """Linear daily close series of length n."""
    return [start + step * i for i in range(n)]


class TestStageClassification:
    def test_stage2_advancing(self):
        # Steadily rising series: price above a rising SMA -> Stage 2.
        closes = _series(200, 10.0, 0.2)  # rises to ~50
        r = classify_weinstein_stage(closes)
        assert r["stage"] == 2
        assert r["above_sma"] is True
        assert r["slope_pct"] > 0

    def test_stage4_declining(self):
        closes = _series(200, 60.0, -0.2)  # steady decline
        r = classify_weinstein_stage(closes)
        assert r["stage"] == 4
        assert r["above_sma"] is False
        assert r["slope_pct"] < 0

    def test_flat_market_is_not_stage2_or_stage4(self):
        # A flat market has no trend: it must be a non-advancing, non-declining
        # stage (1 basing or 3 topping — the exact split needs prior-trend context
        # we don't track, and doesn't matter for the Stage 2 filter).
        closes = [20.0 + (0.05 if i % 2 else -0.05) for i in range(200)]
        r = classify_weinstein_stage(closes)
        assert r["stage"] in (1, 3)
        assert r["slope_pct"] == pytest.approx(0.0, abs=0.5)

    def test_stage3_topping(self):
        # Long advance then a flat top: SMA still slightly rising but price stalls
        # near it. Build advance for 150 bars then plateau for 60.
        closes = _series(150, 10.0, 0.3) + [55.0] * 60
        r = classify_weinstein_stage(closes)
        assert r["stage"] in (2, 3)  # transition zone; not declining/basing

    def test_insufficient_history(self):
        r = classify_weinstein_stage(_series(50, 10.0, 0.2))
        assert r["stage"] is None and "insufficient" in r["reason"]

    def test_is_stage2_helper(self):
        assert is_stage2(_series(200, 10.0, 0.2)) is True
        assert is_stage2(_series(200, 60.0, -0.2)) is False

    def test_handles_none_values(self):
        closes = _series(200, 10.0, 0.2)
        closes_with_gaps = closes[:100] + [None] + closes[100:]
        r = classify_weinstein_stage(closes_with_gaps)
        assert r["stage"] == 2  # None filtered out
