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


@pytest.mark.parametrize("stored, expected", [
    (False, False),
    (True, True),
    (0, False),
    (1, True),
    ("0", False),   # THE historic defect: bool("0") is True. coerce_bool must read it False.
    ("1", True),
    ("true", True),
    ("false", False),
])
def test_use_atr_stop_is_read_through_coerce_bool_not_bool(stored, expected, monkeypatch):
    """Parity test (atr_grid_2027 design §3.2 item 7): the value reaching _ensure_safeguard_stop
    must be a REAL bool for both the shape a GA int gene arrives in (backtest path: 0/1) and the
    shape a stored live setting can arrive in (legacy string spellings, incl. the "1"/"0" defect
    coerce_bool exists for). bool("0") is True; this pins the read goes through coerce_bool
    instead, so the off-by-stringification defect that pinned use_atr_stop off in EVERY run on
    record cannot resurface through this one call site."""
    import ba2_common.core.position_sizing as ps

    seen = {}

    def _fake_synth(price, is_long, risk_pct, *, atr=None, atr_multiplier=2.0, min_stop_pct=7.0,
                    trace=None):
        seen["risk_pct"] = risk_pct
        seen["atr"] = atr
        return price * 0.9

    monkeypatch.setattr(ps, "get_latest_atr", lambda *a, **k: 5.0)
    monkeypatch.setattr(ps, "synthesize_safeguard_stop", _fake_synth)
    rm = TradeRiskManagement()
    expert = _ExpertStub({
        "risk_per_trade_pct": 1.5, "atr_multiplier": 2.0, "atr_period": 14,
        "min_stop_loss_pct": 3.0, "use_atr_stop": stored,
    })
    order = _order(stop_price=None)
    rm._ensure_safeguard_stop(order, "AAPL", current_price=100.0, expert=expert)
    # ATR was fetched (and therefore fed into the synthesiser as non-None) iff the coerced value
    # is True -- an indirect but unambiguous read of what use_atr_stop resolved to.
    assert (seen["atr"] is not None) is expected


def test_safeguard_candidate_is_recorded_on_the_trace():
    """atr_grid_2027 design §3.3: the trace must name WHICH candidate (atr / risk_pct /
    min_stop_floor) the safeguard stop actually came from, next to the existing 'binding'
    fields, so results can report the ATR-bound share of entries. Result-neutral: stop_price is
    unaffected by whether a trace is passed."""
    rm = TradeRiskManagement()
    expert = _ExpertStub({
        "risk_per_trade_pct": 1.5, "atr_multiplier": 2.0, "atr_period": 14,
        "min_stop_loss_pct": 0.0, "use_atr_stop": False,
    })
    order = _order(stop_price=None)
    trace: dict = {}
    rm._ensure_safeguard_stop(order, "AAPL", current_price=100.0, expert=expert, trace=trace)
    # use_atr_stop is off, so no ATR candidate exists -- the risk% distance must win.
    assert trace.get("safeguard_candidate") == "risk_pct"
    assert order.stop_price == pytest.approx(100.0 * (1 - 0.015), abs=0.01)
