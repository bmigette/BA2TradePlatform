"""Review finding 1 (2026-09-09): the live SmartRiskManagerToolkit sized risk-based orders off
``risk_per_trade_pct`` while the classic risk manager (which is the ONLY one a backtest runs)
sized off ``atr_risk_budget_pct``. With atr_risk_budget_pct=0.5 / risk_per_trade_pct=5 on an
$18 000 virtual balance the same config bought 18 shares in the backtest and 180 shares live --
a 10x divergence between what was optimized and what trades.

Both must now go through the ONE shared resolver,
``ba2_common.core.position_sizing.resolve_sizing_risk_budget_pct``. The classic side is pinned
byte-identical in packages/common/tests/test_position_sizing.py.
"""
from __future__ import annotations

import logging

from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.types import OrderDirection


class _StubExpert:
    """Minimal expert exposing only what the sizing paths read. Mirrors the review probe:
    $18 000 virtual equity, a 0.5% sizing budget and a 5% stop-distance gene."""

    def __init__(self, **overrides):
        self._settings = {
            "atr_risk_budget_pct": 0.5,
            "risk_per_trade_pct": 5.0,
            "atr_multiplier": 2.0,
            "atr_period": 14,
            "min_stop_loss_pct": 0.0,
            "use_atr_stop": False,
            "max_virtual_equity_per_instrument_percent": 100.0,
        }
        self._settings.update(overrides)

    def get_virtual_balance(self):
        return 18_000.0

    def get_available_balance(self):
        return 18_000.0

    def get_setting_with_interface_default(self, key, log_warning=True):
        return self._settings[key]


def _make_toolkit(**setting_overrides):
    # Bypass the DB-backed __init__; only the sizing/stop-synthesis paths are exercised.
    tk = object.__new__(SmartRiskManagerToolkit)
    tk.expert = _StubExpert(**setting_overrides)
    tk.logger = logging.getLogger("test_smart_rm_sizing_budget")
    tk.get_current_price = lambda symbol: 100.0
    return tk


def test_auto_size_uses_the_sizing_budget_not_the_stop_gene():
    """0.5% of $18 000 = $90 risk over a $5 stop distance = 18 shares -- the same number the
    classic RM produces for this config. Reading risk_per_trade_pct instead gave 180."""
    tk = _make_toolkit()
    result = tk._auto_size_by_risk("NEW", OrderDirection.BUY, sl_price=95.0)
    assert result["quantity"] == 18, (
        f"Smart RM must size off the resolved budget (0.5%), got {result['quantity']}")
    assert result["risk_dollars"] == 90.0


def test_auto_size_falls_back_to_risk_per_trade_pct_when_budget_unset():
    """Unchanged behaviour for a config that never declared the budget gene: 5% of $18 000 =
    $900 over a $5 stop = 180 shares."""
    tk = _make_toolkit(atr_risk_budget_pct=None)
    result = tk._auto_size_by_risk("NEW", OrderDirection.BUY, sl_price=95.0)
    assert result["quantity"] == 180
    assert result["risk_dollars"] == 900.0


def test_explicit_quantity_stop_synthesis_uses_the_resolved_budget(monkeypatch):
    """The explicit-quantity path synthesizes the missing SL at the price where the loss equals
    the risk budget. It read risk_per_trade_pct (5.0) directly, so an agent-specified quantity
    got a stop sized for 10x the dollar risk the strategy was optimized on."""
    captured = {}

    def fake_derive_stop_for_quantity(equity, entry_price, quantity, risk_per_trade_pct,
                                      is_long, *, min_stop_pct=7.0):
        captured.update(equity=equity, entry_price=entry_price, quantity=quantity,
                        risk_pct=risk_per_trade_pct, is_long=is_long, min_stop_pct=min_stop_pct)
        return {"sl_price": 95.0, "quantity": quantity, "rejected": False,
                "reason": "", "stop_pct": 5.0}

    import ba2_trade_platform.core.position_sizing as ps
    monkeypatch.setattr(ps, "derive_stop_for_quantity", fake_derive_stop_for_quantity)

    tk = _make_toolkit()
    out = tk._synthesize_stop_for_explicit_quantity("NEW", 10, OrderDirection.BUY)

    assert captured["risk_pct"] == 0.5, (
        f"stop synthesis must use the RESOLVED budget, got {captured['risk_pct']}")
    assert captured["equity"] == 18_000.0
    assert captured["entry_price"] == 100.0
    assert captured["quantity"] == 10
    assert captured["is_long"] is True
    # min_stop_loss_pct is 0.0 here and this path has always read it as `float(x or 7.0)`;
    # that historical fallback is deliberately left alone (only the budget moves in this fix).
    assert captured["min_stop_pct"] == 7.0
    assert out["sl_price"] == 95.0
