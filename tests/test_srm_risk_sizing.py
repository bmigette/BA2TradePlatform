"""Unit tests for the Smart Risk Manager risk-based position sizing helper."""

from ba2_trade_platform.core.SmartRiskManagerToolkit import compute_risk_based_quantity


def test_basic_risk_sizing():
    # 5% of $10,000 = $500 risk budget; stop distance $8 -> floor(500/8) = 62 shares
    assert compute_risk_based_quantity(100.0, 92.0, 10000.0, 5.0) == 62


def test_sizing_works_for_short_side_distance():
    # SL above entry (short): distance is |100 - 108| = 8 -> same 62 shares
    assert compute_risk_based_quantity(100.0, 108.0, 10000.0, 5.0) == 62


def test_tighter_stop_gives_larger_size():
    wide = compute_risk_based_quantity(100.0, 90.0, 10000.0, 5.0)   # dist 10 -> 50
    tight = compute_risk_based_quantity(100.0, 98.0, 10000.0, 5.0)  # dist 2  -> 250
    assert tight > wide
    assert wide == 50 and tight == 250


def test_disabled_when_pct_zero_or_none():
    assert compute_risk_based_quantity(100.0, 92.0, 10000.0, 0) is None
    assert compute_risk_based_quantity(100.0, 92.0, 10000.0, None) is None


def test_none_when_no_stop_loss():
    assert compute_risk_based_quantity(100.0, None, 10000.0, 5.0) is None


def test_none_on_zero_stop_distance():
    assert compute_risk_based_quantity(100.0, 100.0, 10000.0, 5.0) is None


def test_none_on_nonpositive_prices_or_equity():
    assert compute_risk_based_quantity(0.0, 92.0, 10000.0, 5.0) is None
    assert compute_risk_based_quantity(None, 92.0, 10000.0, 5.0) is None
    assert compute_risk_based_quantity(100.0, 92.0, 0.0, 5.0) is None


def test_none_when_budget_below_one_share():
    # 5% of $50 = $2.50 budget; stop distance $8 -> floor(0.31) = 0 -> None
    assert compute_risk_based_quantity(100.0, 92.0, 50.0, 5.0) is None
