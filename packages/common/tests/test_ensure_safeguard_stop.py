"""Regression: the safeguard stop-loss must attach REGARDLESS of sizing_mode.

Previously the safeguard SL (``synthesize_safeguard_stop``) was only ever synthesized inside
``_risk_atr_quantity``, which is only called when ``sizing_mode == 'risk_atr'``. An expert left
on the default ``notional`` sizing mode (e.g. a live instance that never explicitly set
``sizing_mode``) could hold an entry whose ruleset set no explicit SL with literally zero
downside protection. ``_ensure_safeguard_stop`` is now the single, sizing-mode-independent
entry point for this, called both from the notional path in ``_calculate_order_quantities``
and from ``_risk_atr_quantity``."""
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeRiskManagement import TradeRiskManagement
from ba2_common.core.types import OrderDirection


class _ExpertStub:
    """Minimal expert stub: only exercises get_setting_with_interface_default lookups
    ``_ensure_safeguard_stop`` needs. ``use_atr_stop=False`` avoids needing an
    indicator_provider (get_latest_atr is never called)."""

    def __init__(self, settings):
        self._settings = settings

    def get_setting_with_interface_default(self, key, log_warning=False):
        return self._settings.get(key)


def _order(stop_price=None, side=OrderDirection.BUY):
    return SimpleNamespace(stop_price=stop_price, side=side)


def test_notional_mode_order_with_no_explicit_sl_gets_safeguard():
    """The exact instance-6-shaped gap: sizing_mode='notional' (or unset), no SL from the
    ruleset. Must still end up with a protective stop_price."""
    rm = TradeRiskManagement()
    expert = _ExpertStub({
        "risk_per_trade_pct": 1.5,
        "atr_multiplier": 2.0,
        "atr_period": 14,
        "min_stop_loss_pct": 14.0,
        "use_atr_stop": False,
    })
    order = _order(stop_price=None)
    rm._ensure_safeguard_stop(order, "PKE", current_price=34.96, expert=expert)
    # risk% (1.5%) < min_stop_pct floor (14%) -> floor dominates, same formula as live's PKE fix
    # (synthesize_safeguard_stop rounds to 2dp).
    assert order.stop_price == pytest.approx(34.96 * (1 - 0.14), abs=0.01)


def test_explicit_ruleset_sl_is_never_overwritten():
    """A ruleset that DID set an explicit SL must win -- the safeguard only fills a gap,
    never replaces an intentional stop."""
    rm = TradeRiskManagement()
    expert = _ExpertStub({
        "risk_per_trade_pct": 1.5, "atr_multiplier": 2.0, "atr_period": 14,
        "min_stop_loss_pct": 14.0, "use_atr_stop": False,
    })
    order = _order(stop_price=32.0)
    rm._ensure_safeguard_stop(order, "PKE", current_price=34.96, expert=expert)
    assert order.stop_price == 32.0


def test_short_order_safeguard_stop_is_above_price():
    rm = TradeRiskManagement()
    expert = _ExpertStub({
        "risk_per_trade_pct": 1.5, "atr_multiplier": 2.0, "atr_period": 14,
        "min_stop_loss_pct": 14.0, "use_atr_stop": False,
    })
    order = _order(stop_price=None, side=OrderDirection.SELL)
    rm._ensure_safeguard_stop(order, "CALX", current_price=36.80, expert=expert)
    assert order.stop_price == pytest.approx(36.80 * (1 + 0.14), abs=0.01)
