"""min_take_profit_percent floor on AdjustTakeProfitAction (TradeActions.py).

Mirrors AdjustStopLossAction's existing minimum-distance enforcement, but relative to the REAL
entry fill price (order.limit_price -> order.open_price -> current-price fallback), not current
price -- a take-profit is fundamentally "how far above cost basis is worth locking in," unlike a
trailing stop which is legitimately current-price-relative. Covers both call paths that compute
a TP: execute()'s _enforce_minimum_distance() (the live path) and compute_price() (used by the
backtest engine's entry_action seeding).
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import (
    AdjustTakeProfitAction, compute_tp_floor_price, resolve_min_take_profit_pct)
from ba2_common.core.types import OrderRecommendation


class _FakeAccount:
    """Minimal account stub -- these tests never need a live current price (entry_price always
    resolves from order.open_price), but TradeAction.__init__ requires an account object."""
    def get_instrument_current_price(self, symbol, price_type=None):
        raise AssertionError("current price should not be needed when order.open_price is set")


def _order(open_price=100.0, side="BUY", limit_price=None, expert_recommendation_id=None):
    return SimpleNamespace(
        id=1, symbol="AAPL", side=side, limit_price=limit_price, open_price=open_price,
        expert_recommendation_id=expert_recommendation_id,
    )


def _tp_action(order, target_price=None, expert_recommendation=None, order_recommendation=OrderRecommendation.BUY,
              reference_value=None, percent=None):
    return AdjustTakeProfitAction(
        "AAPL", _FakeAccount(), order_recommendation, existing_order=order,
        expert_recommendation=expert_recommendation, take_profit_price=target_price,
        reference_value=reference_value, percent=percent,
    )


# ---------------------------------------------------------------------------
# _enforce_minimum_distance (the live execute() path)
# ---------------------------------------------------------------------------
def test_tp_floor_pushes_out_when_below_minimum():
    """entry=100, target=100.5 (0.5%) -- default min 2% must push it to 102.0."""
    order = _order(open_price=100.0)
    action = _tp_action(order, target_price=100.5)
    action._enforce_minimum_distance()
    assert action.target_price == pytest.approx(102.0)


def test_tp_floor_noop_when_already_above_minimum():
    order = _order(open_price=100.0)
    action = _tp_action(order, target_price=110.0)  # 10% -- well above the 2% default floor
    action._enforce_minimum_distance()
    assert action.target_price == pytest.approx(110.0)


def test_tp_floor_uses_real_fill_price_not_a_different_reference():
    """The floor is computed off order.open_price (the REAL fill) -- verified here by using an
    open_price that would give a DIFFERENT floor than some other candidate reference price."""
    order = _order(open_price=200.0)  # real fill was $200, not e.g. a $100 signal price
    action = _tp_action(order, target_price=201.0)  # 0.5% off the REAL fill -> must be floored
    action._enforce_minimum_distance()
    assert action.target_price == pytest.approx(200.0 * 1.02)  # floored relative to $200, not $100


def test_tp_floor_respects_custom_min_take_profit_percent_from_recommendation():
    order = _order(open_price=100.0)
    rec = SimpleNamespace(min_take_profit_percent=5.0)
    action = _tp_action(order, target_price=101.0, expert_recommendation=rec)  # 1% < 5% floor
    action._enforce_minimum_distance()
    assert action.target_price == pytest.approx(105.0)


def test_tp_floor_defaults_to_2_pct_when_recommendation_missing_field():
    """A recommendation object created before this field existed (plain object, no attribute)
    must still floor at the 2.0 default, not crash on getattr."""
    order = _order(open_price=100.0)
    rec = SimpleNamespace()  # no min_take_profit_percent attribute at all
    action = _tp_action(order, target_price=100.5, expert_recommendation=rec)
    action._enforce_minimum_distance()
    assert action.target_price == pytest.approx(102.0)


def test_tp_floor_short_position_direction():
    """SELL/short: TP sits BELOW entry: entry=100, target=99.5 (0.5%) -- floored to 98.0."""
    order = _order(open_price=100.0, side="SELL")
    action = _tp_action(order, target_price=99.5, order_recommendation=OrderRecommendation.SELL)
    action._enforce_minimum_distance()
    assert action.target_price == pytest.approx(98.0)


def test_tp_floor_resolves_recommendation_via_order_when_not_passed_directly():
    """When expert_recommendation isn't passed to the action constructor (only linked via the
    order's expert_recommendation_id), the floor still resolves the custom percent by loading
    the recommendation row."""
    from ba2_common.core import db
    from ba2_common.core.models import ExpertRecommendation
    from ba2_common.core.types import RiskLevel, TimeHorizon
    import tempfile
    import os

    db_path = os.path.join(tempfile.mkdtemp(), "tp_floor.sqlite")
    db.configure_db(db_path)
    db.init_db()
    from ba2_common.core.db import add_instance
    rec = ExpertRecommendation(
        instance_id=1, symbol="AAPL", recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=8.0, price_at_date=100.0, min_take_profit_percent=6.0,
        risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM,
    )
    rec_id = add_instance(rec)

    order = _order(open_price=100.0, expert_recommendation_id=rec_id)
    action = _tp_action(order, target_price=101.0)  # 1% < 6% -> must floor
    action._enforce_minimum_distance()
    assert action.target_price == pytest.approx(106.0)


# ---------------------------------------------------------------------------
# compute_price (the backtest entry_action-seeding path)
# ---------------------------------------------------------------------------
def test_compute_price_applies_same_floor():
    order = _order(open_price=100.0)
    action = _tp_action(order, reference_value="order_open_price", percent=0.5)  # 0.5% -> floor
    price = action.compute_price(order)
    assert price == pytest.approx(102.0)


def test_compute_price_noop_when_already_above_minimum():
    order = _order(open_price=100.0)
    action = _tp_action(order, reference_value="order_open_price", percent=10.0)
    price = action.compute_price(order)
    assert price == pytest.approx(110.0)


# ---------------------------------------------------------------------------
# compute_tp_floor_price / resolve_min_take_profit_pct (the shared pure helpers --
# used both by the two paths above AND by TradeManager's post-fill floor re-check)
# ---------------------------------------------------------------------------
class TestComputeTpFloorPrice:
    def test_returns_floor_when_below_minimum_long(self):
        assert compute_tp_floor_price(100.5, 100.0, 2.0, is_long=True) == pytest.approx(102.0)

    def test_returns_none_when_already_above_minimum_long(self):
        assert compute_tp_floor_price(110.0, 100.0, 2.0, is_long=True) is None

    def test_returns_floor_when_below_minimum_short(self):
        assert compute_tp_floor_price(99.5, 100.0, 2.0, is_long=False) == pytest.approx(98.0)

    def test_returns_none_when_already_above_minimum_short(self):
        assert compute_tp_floor_price(90.0, 100.0, 2.0, is_long=False) is None

    def test_handles_a_target_below_entry_long(self):
        """This is exactly CALX's case: a raw target BELOW entry (a guaranteed loss for a long)
        must still be floored up, not just nudged."""
        assert compute_tp_floor_price(97.0, 100.0, 2.0, is_long=True) == pytest.approx(102.0)

    def test_returns_none_on_falsy_entry_price(self):
        assert compute_tp_floor_price(100.5, 0.0, 2.0, is_long=True) is None
        assert compute_tp_floor_price(100.5, None, 2.0, is_long=True) is None


class TestResolveMinTakeProfitPct:
    def test_returns_default_when_no_recommendation_id(self):
        assert resolve_min_take_profit_pct(None) == 2.0
        assert resolve_min_take_profit_pct(0) == 2.0

    def test_returns_default_when_recommendation_not_found(self):
        assert resolve_min_take_profit_pct(999999) == 2.0

    def test_returns_custom_value_from_recommendation(self):
        from ba2_common.core import db
        from ba2_common.core.db import add_instance
        from ba2_common.core.models import ExpertRecommendation
        from ba2_common.core.types import RiskLevel, TimeHorizon
        import tempfile
        import os

        db_path = os.path.join(tempfile.mkdtemp(), "resolve_min_tp.sqlite")
        db.configure_db(db_path)
        db.init_db()
        rec = ExpertRecommendation(
            instance_id=1, symbol="AAPL", recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=8.0, price_at_date=100.0, min_take_profit_percent=6.0,
            risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM,
        )
        rec_id = add_instance(rec)

        assert resolve_min_take_profit_pct(rec_id) == 6.0
