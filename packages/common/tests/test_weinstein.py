"""Unit tests for the pure Weinstein stage classifier (ba2_common.core.weinstein).

The real public API (reconciled from source) is:
    classify_weinstein_stage(closes, sma_period=150, slope_lookback=20,
                             flat_threshold_pct=0.5) -> dict
        returns {"stage": 1-4 or None, "sma", "slope_pct", "price",
                 "above_sma", "reason"}
    is_stage2(closes, **kwargs) -> bool

It needs at least sma_period + slope_lookback (default 170) daily closes to
classify; fewer returns stage=None with an "insufficient history" reason.
"""


def test_weinstein_module_imports_without_providers():
    import ba2_common.core.weinstein as w
    assert w is not None
    assert hasattr(w, "classify_weinstein_stage")
    assert hasattr(w, "is_stage2")


def test_weinstein_stage2_uptrend():
    """A clean rising series above a rising 150-period SMA classifies as Stage 2."""
    from ba2_common.core import weinstein
    closes = [10 + i * 0.5 for i in range(200)]   # steady uptrend, enough history
    res = weinstein.classify_weinstein_stage(closes)
    assert isinstance(res, dict)
    assert res["stage"] == 2          # advancing — the buy zone
    assert res["above_sma"] is True
    assert res["slope_pct"] > 0       # SMA rising
    assert weinstein.is_stage2(closes) is True


def test_weinstein_stage4_downtrend():
    """A clean falling series below a falling SMA classifies as Stage 4."""
    from ba2_common.core import weinstein
    closes = [200 - i * 0.5 for i in range(200)]   # steady downtrend
    res = weinstein.classify_weinstein_stage(closes)
    assert res["stage"] == 4
    assert res["above_sma"] is False
    assert res["slope_pct"] < 0
    assert weinstein.is_stage2(closes) is False


def test_weinstein_insufficient_history_returns_none():
    """Too few bars -> stage None with an explanatory reason, no exception."""
    from ba2_common.core import weinstein
    res = weinstein.classify_weinstein_stage([1.0, 2.0, 3.0])
    assert res["stage"] is None
    assert "insufficient" in res["reason"].lower()


def test_weinstein_sma_helper():
    """Internal _sma returns the simple mean of the trailing window, None if short."""
    from ba2_common.core import weinstein
    assert weinstein._sma([1.0, 2.0, 3.0, 4.0], 2) == 3.5   # mean of last two
    assert weinstein._sma([1.0], 5) is None
