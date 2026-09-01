"""``max_drawdown_from_pnl``: the worst peak-to-trough fall, and when a % is meaningless.

``calculate_max_drawdown`` already existed and divides by the running maximum — which is
exactly 0 at the start of any cumulative P&L series, and stays 0 or negative for an
expert that is down on its first trades. So the first drawdown it meets is a
divide-by-zero and every later one is measured against a negative base. This one walks
the curve itself and reports the percentage as None when there is no positive peak.
"""
import pytest

from ba2_trade_platform.ui.components.performance_charts import max_drawdown_from_pnl


def test_no_trades_has_no_drawdown_and_no_percentage():
    assert max_drawdown_from_pnl([]) == (0.0, None)


def test_a_curve_that_only_rises_has_a_zero_drawdown_and_a_real_zero_percent():
    """0% with a positive peak is a MEASUREMENT ("never fell"), and is deliberately
    distinct from the None that means "no peak to measure against"."""
    assert max_drawdown_from_pnl([10.0, 5.0, 20.0]) == (0.0, 0.0)


def test_the_fall_is_measured_from_the_peak_not_from_the_start():
    # cumulative: 100, 60 -> peak 100, trough 60 -> fall 40 = 40%
    assert max_drawdown_from_pnl([100.0, -40.0]) == (40.0, 40.0)


def test_a_later_recovery_does_not_shrink_an_earlier_drawdown():
    """Measured against the peak IN FORCE AT THE TIME: a 30% fall early on does not
    become a 5% one because the curve tripled afterwards."""
    # 100 -> 70 (30% off a peak of 100), then up to 1000
    dollars, pct = max_drawdown_from_pnl([100.0, -30.0, 930.0])

    assert dollars == 30.0
    assert pct == 30.0


def test_the_worst_of_several_drawdowns_wins():
    # 100 -> 90 (10%), then 200 -> 100 (50%)
    dollars, pct = max_drawdown_from_pnl([100.0, -10.0, 110.0, -100.0])

    assert dollars == 100.0
    assert pct == 50.0


def test_an_expert_never_in_profit_has_dollars_but_no_percentage():
    """"Down 100% of a peak of -$40" is not a fact. The dollar figure is the only
    honest answer, which is why both are returned."""
    dollars, pct = max_drawdown_from_pnl([-10.0, -30.0])

    assert dollars == 40.0
    assert pct is None


def test_a_drawdown_that_begins_before_the_first_profit_is_still_measured_later():
    """Down first, then above water, then down again: the percentage exists from the
    moment there is a positive peak, and describes the fall from THAT peak."""
    # -20, then +120 -> cumulative 100 (peak 100), then -50 -> 50 = 50%
    dollars, pct = max_drawdown_from_pnl([-20.0, 120.0, -50.0])

    assert pct == 50.0
    # The deepest fall in DOLLARS is the later one: peak 100 down to 50. The opening
    # -20 is a fall of 20 from a starting cumulative of 0, which is smaller.
    assert dollars == 50.0
