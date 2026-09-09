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
    # The floor actually applied comes back in the dict so the caller's log reports the number
    # this method used instead of re-reading the setting.
    assert out["min_stop_pct"] == 7.0


def test_explicit_quantity_stop_reduces_qty_with_real_arithmetic():
    """The same path with NO stubbing, so the pinned numbers are the real function's.

    Budget 0.5% of $18 000 = $90 of dollar risk. min_stop_loss_pct 7% on a $100 price is a $7
    floor, so at most int(90 // 7) = 12 shares can be held without the 7% stop costing more than
    the budget: an explicit 100 shares is cut to 12, and the stop lands where 12 shares lose
    exactly $90 -- $90/12 = $7.50 below the entry.
    """
    tk = _make_toolkit(min_stop_loss_pct=7.0)
    out = tk._synthesize_stop_for_explicit_quantity("NEW", 100, OrderDirection.BUY)
    assert out["rejected"] is False
    assert out["quantity"] == 12
    assert out["sl_price"] == 92.5
    assert out["stop_pct"] == 7.5
    assert out["min_stop_pct"] == 7.0


def test_explicit_quantity_stop_rejects_when_one_share_risks_too_much():
    """A 0.01% budget on $18 000 is $1.80 of dollar risk, but the 7% floor makes one share of a
    $100 name risk $7 -- no quantity fits, so the order is REJECTED rather than silently given a
    stop tighter than the floor."""
    tk = _make_toolkit(atr_risk_budget_pct=0.01, min_stop_loss_pct=7.0)
    out = tk._synthesize_stop_for_explicit_quantity("NEW", 1, OrderDirection.BUY)
    assert out["rejected"] is True
    assert out["sl_price"] is None
    assert out["reason"]


def test_auto_size_splits_budget_for_size_from_distance_gene_for_stop():
    """PIN THE SPLIT so a future cleanup cannot re-collapse the two genes onto one setting.

    No SL given and use_atr_stop off, so the safeguard stop is synthesized purely from the
    risk_per_trade_pct DISTANCE gene: 5% of $100 = $95.00. The share count over that distance
    then comes from the BUDGET: 0.5% of $18 000 = $90 / $5 = 18 shares. Reading one gene for
    both jobs gives 180 shares (budget used as distance) or a $99.50 stop (distance from the
    budget) -- neither of which this asserts.
    """
    tk = _make_toolkit()
    result = tk._auto_size_by_risk("NEW", OrderDirection.BUY, sl_price=None)
    assert result["quantity"] == 18, "SIZE must come from the 0.5% budget"
    assert result["implied_sl"] == 95.0, "STOP DISTANCE must come from the 5% risk_per_trade_pct gene"
    assert result["risk_dollars"] == 90.0
    assert result["risk_per_share"] == 5.0
