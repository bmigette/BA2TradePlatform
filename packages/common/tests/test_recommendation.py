from ba2_common.core.types import Recommendation, OrderRecommendation


def test_recommendation_skip_is_first_class():
    r = Recommendation(signal=OrderRecommendation.HOLD, confidence=0.0,
                       current_price=10.0, skip=True, skip_reason="no coverage")
    assert r.skip is True and r.skip_reason == "no coverage"


def test_recommendation_target_price_field():
    """The Recommendation value object carries an optional target_price (Prereq 2).

    Defaults to None so non-target experts fall back to expected_profit_percent in the
    backtest bracket; round-trips when an expert (e.g. FMPRating) sets it.
    """
    r = Recommendation(signal=OrderRecommendation.BUY, confidence=80.0,
                       current_price=100.0, expected_profit_percent=8.0,
                       target_price=120.0)
    assert r.target_price == 120.0

    r2 = Recommendation(signal=OrderRecommendation.BUY, confidence=80.0,
                        current_price=100.0, expected_profit_percent=8.0)
    assert r2.target_price is None


def test_almost_equals_float_tolerant():
    a = Recommendation(OrderRecommendation.BUY, 78.10000001, 100.0, "x", 8.0)
    b = Recommendation(OrderRecommendation.BUY, 78.1, 100.0, "x", 8.0)
    assert a.almost_equals(b)


def test_almost_equals_detects_signal_drift():
    a = Recommendation(OrderRecommendation.BUY, 78.1, 100.0, "x", 8.0)
    b = Recommendation(OrderRecommendation.HOLD, 78.1, 100.0, "x", 8.0)
    assert not a.almost_equals(b)


def test_live_provider_bundle_price_at_date(monkeypatch):
    import pandas as pd
    from ba2_common.core.backtest_context import LiveProviderBundle

    class FakeOHLCV:
        def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
            return pd.DataFrame({"Close": [9.0, 10.5]})
    bundle = LiveProviderBundle(lambda cat, name, **kw: FakeOHLCV())
    assert bundle.price_at_date("AAPL", None) == 10.5


def _minimal_expert_recommendation(**overrides):
    """Build an ExpertRecommendation with all non-nullable fields populated."""
    from ba2_common.core.models import ExpertRecommendation
    from ba2_common.core.types import OrderRecommendation, RiskLevel, TimeHorizon

    kwargs = dict(
        instance_id=1,
        symbol="AAPL",
        recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=8.0,
        price_at_date=100.0,
        risk_level=RiskLevel.MEDIUM,
        time_horizon=TimeHorizon.MEDIUM_TERM,
    )
    kwargs.update(overrides)
    return ExpertRecommendation(**kwargs)


def test_expert_recommendation_target_price_field():
    """target_price round-trips and defaults to None (RE3 / Prereq 2)."""
    r = _minimal_expert_recommendation(target_price=200.0)
    assert r.target_price == 200.0

    r2 = _minimal_expert_recommendation()
    assert r2.target_price is None
