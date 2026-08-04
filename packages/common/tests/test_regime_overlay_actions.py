"""The regime overlay actually moves the TP/SL PRICE the adjust actions produce.

This file exists because of a specific past failure: ``use_atr_stop`` was searched by the GA for
months while the code path it named did nothing. Asserting that a multiplier was *computed* would
have passed then too. So every test here asserts the resulting PRICE -- the thing the broker is
sent -- and the doubling test compares it against the unscaled run rather than a hand-copied
constant.

Both percent->price sites are covered, because there are two and they are reached by different
callers: ``compute_price`` (the merged TP+SL path TradeActionEvaluator uses for entry brackets --
the common case in a backtest) and ``execute``'s inline calculation (a single TP-or-SL
adjustment). A mixin on the subclasses would have covered only the second.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import AdjustStopLossAction, AdjustTakeProfitAction
from ba2_common.core.instance_resolver import (
    _UnconfiguredResolver, get_instance_resolver, set_instance_resolver)
from ba2_common.core.regime_overlay import reset_stressed, set_stressed
from ba2_common.core.types import OrderRecommendation

EXPERT_ID = 42


class _Expert:
    def __init__(self, **settings):
        self._settings = settings

    def get_setting_with_interface_default(self, name, log_warning=True):
        return self._settings.get(name)


class _Resolver:
    def __init__(self, expert):
        self._expert = expert

    def get_expert_instance(self, expert_id):
        return self._expert if expert_id == EXPERT_ID else None

    def get_account_instance(self, account_id):
        raise AssertionError("not used")

    def get_account_instance_from_transaction(self, transaction):
        raise AssertionError("not used")


class _FakeAccount:
    """The SL floor path asks for a current price; the TP path never should."""
    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0


def _order(side="BUY"):
    return SimpleNamespace(id=1, symbol="AAPL", side=side, limit_price=None, open_price=100.0,
                           expert_recommendation_id=None)


def _rec():
    return SimpleNamespace(instance_id=EXPERT_ID, min_take_profit_percent=0.0)


def _install(**settings):
    set_instance_resolver(_Resolver(_Expert(**settings)))


@pytest.fixture(autouse=True)
def _isolate():
    previous = get_instance_resolver()
    reset_stressed()
    yield
    reset_stressed()
    set_instance_resolver(previous)      # restore, don't clobber, the process-wide seam


def _tp_price(percent=10.0, stressed=None, **settings):
    _install(**settings)
    set_stressed(stressed)
    order = _order()
    action = AdjustTakeProfitAction(
        "AAPL", _FakeAccount(), OrderRecommendation.BUY, existing_order=order,
        expert_recommendation=_rec(), reference_value="order_open_price", percent=percent)
    return action.compute_price(order)


def _sl_price(percent=-9.0, stressed=None, side="BUY", **settings):
    _install(**settings)
    set_stressed(stressed)
    order = _order(side=side)
    action = AdjustStopLossAction(
        "AAPL", _FakeAccount(), OrderRecommendation.BUY if side == "BUY" else OrderRecommendation.SELL,
        existing_order=order, expert_recommendation=_rec(),
        reference_value="order_open_price", percent=percent)
    return action.compute_price(order)


_ON = dict(regime_overlay_enabled=True, regime_tp_scale=2.0, regime_stop_scale=2.0)


# --------------------------------------------------------------------------------------------
# The headline assertion: the SUBMITTED distance doubles
# --------------------------------------------------------------------------------------------

def test_tp_distance_doubles_when_stressed():
    """+10% from a $100 entry must become +20% at regime_tp_scale=2.0 -- measured as a distance
    ratio against the neutral run, so this cannot pass on a stale hand-written constant."""
    base = _tp_price(percent=10.0, stressed=False, **_ON)
    scaled = _tp_price(percent=10.0, stressed=True, **_ON)
    assert (scaled - 100.0) == pytest.approx(2.0 * (base - 100.0))
    assert scaled == pytest.approx(120.0)


def test_sl_distance_doubles_when_stressed():
    base = _sl_price(percent=-9.0, stressed=False, **_ON)
    scaled = _sl_price(percent=-9.0, stressed=True, **_ON)
    assert (100.0 - scaled) == pytest.approx(2.0 * (100.0 - base))


def test_scale_below_one_tightens_the_tp():
    """Two-sided range: 0.5 must HALVE the distance, not be clamped to neutral."""
    scaled = _tp_price(percent=10.0, stressed=True,
                       regime_overlay_enabled=True, regime_tp_scale=0.5)
    assert scaled == pytest.approx(105.0)


def test_short_position_tp_scales_downward():
    """A short's TP sits BELOW entry; scaling must widen the distance, not flip the direction."""
    _install(**_ON)
    set_stressed(True)
    order = _order(side="SELL")
    action = AdjustTakeProfitAction(
        "AAPL", _FakeAccount(), OrderRecommendation.SELL, existing_order=order,
        expert_recommendation=_rec(), reference_value="order_open_price", percent=10.0)
    assert action.compute_price(order) == pytest.approx(80.0)


# --------------------------------------------------------------------------------------------
# Every way the overlay must stay an EXACT no-op
# --------------------------------------------------------------------------------------------

def test_unstressed_bar_is_unchanged():
    assert _tp_price(percent=10.0, stressed=False, **_ON) == pytest.approx(110.0)


def test_unpublished_regime_is_unchanged():
    """No host published a regime (None) -> today's arithmetic, not a guessed scale."""
    assert _tp_price(percent=10.0, stressed=None, **_ON) == pytest.approx(110.0)


def test_disabled_overlay_is_unchanged_even_when_stressed():
    assert _tp_price(percent=10.0, stressed=True,
                     regime_overlay_enabled=False, regime_tp_scale=2.0) == pytest.approx(110.0)


def test_genome_without_the_settings_is_unchanged():
    """Every ruleset persisted before this feature existed has none of these keys."""
    assert _tp_price(percent=10.0, stressed=True) == pytest.approx(110.0)


def test_leak_check_neutral_scale_equals_disabled():
    on = _tp_price(percent=10.0, stressed=True,
                   regime_overlay_enabled=True, regime_tp_scale=1.0)
    off = _tp_price(percent=10.0, stressed=True,
                    regime_overlay_enabled=False, regime_tp_scale=1.0)
    assert on == off == pytest.approx(110.0)


def test_unresolvable_expert_is_unchanged():
    """No resolver injected (ba2_common used outside the host app) raises
    InstanceResolverNotConfigured. That must degrade to today's price, never break order
    placement -- the overlay is an optimisation, not a prerequisite."""
    set_instance_resolver(_UnconfiguredResolver())
    set_stressed(True)
    order = _order()
    action = AdjustTakeProfitAction(
        "AAPL", _FakeAccount(), OrderRecommendation.BUY, existing_order=order,
        expert_recommendation=_rec(), reference_value="order_open_price", percent=10.0)
    assert action.compute_price(order) == pytest.approx(110.0)


def test_explicit_target_price_is_never_scaled():
    """The scale multiplies a percent OFFSET. A rule that names an absolute price means it."""
    _install(**_ON)
    set_stressed(True)
    order = _order()
    action = AdjustTakeProfitAction(
        "AAPL", _FakeAccount(), OrderRecommendation.BUY, existing_order=order,
        expert_recommendation=_rec(), take_profit_price=115.0)
    assert action.compute_price(order) == pytest.approx(115.0)


# --------------------------------------------------------------------------------------------
# The OTHER percent->price site: execute()'s inline calculation
# --------------------------------------------------------------------------------------------

def test_execute_path_scales_too():
    """execute() computes the price inline rather than via compute_price, so it needs its own
    proof. Driven through _regime_scaled_percent (the shared helper both sites call) because
    execute() itself requires a broker/transaction; the assertion is still on the percent that
    the price is derived from one line later."""
    _install(**_ON)
    set_stressed(True)
    action = AdjustTakeProfitAction(
        "AAPL", _FakeAccount(), OrderRecommendation.BUY, existing_order=_order(),
        expert_recommendation=_rec(), reference_value="order_open_price", percent=10.0)
    assert action._regime_scaled_percent() == pytest.approx(20.0)

    set_stressed(False)
    assert action._regime_scaled_percent() == pytest.approx(10.0)
