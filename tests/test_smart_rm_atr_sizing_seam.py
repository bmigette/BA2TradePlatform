"""Phase 6 Task 6 regression: the live SmartRiskManagerToolkit must thread an
injected indicator provider into ba2_common.core.position_sizing.get_latest_atr.

After Task 5 shimmed ``ba2_trade_platform.core.position_sizing`` onto the package,
``get_latest_atr`` requires ``indicator_provider`` as the 2nd positional argument.
The live ``_auto_size_by_risk`` previously called ``get_latest_atr(symbol,
period=...)`` (the pre-extraction signature), which now raises ``TypeError`` and
would silently degrade ATR-based sizing to quantity 0. Task 6 threads the host
default indicator provider, mirroring the classic RM's ``_risk_atr_quantity``.
"""
from __future__ import annotations

import logging

from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.types import OrderDirection


class _StubExpert:
    """Minimal expert exposing only what _auto_size_by_risk reads."""

    def get_virtual_balance(self):
        return 100_000.0

    def get_available_balance(self):
        return 100_000.0

    def get_setting_with_interface_default(self, key, log_warning=True):
        return {
            "risk_per_trade_pct": 1.0,
            "atr_multiplier": 2.0,
            "atr_period": 14,
            "min_stop_loss_pct": 0.0,
            "max_virtual_equity_per_instrument_percent": 10.0,
        }.get(key)


def _make_toolkit():
    # Bypass the DB-backed __init__; we only exercise _auto_size_by_risk.
    tk = object.__new__(SmartRiskManagerToolkit)
    tk.expert = _StubExpert()
    tk.logger = logging.getLogger("test_smart_rm_atr_sizing_seam")
    tk.get_current_price = lambda symbol: 100.0
    return tk


def test_auto_size_no_sl_threads_indicator_provider(monkeypatch):
    """With no SL price, the ATR path runs: the toolkit must build the host
    indicator provider and pass it as the 2nd positional arg to get_latest_atr
    (it must NOT call the old 1-arg signature, which now TypeErrors)."""
    captured = {}

    sentinel_provider = object()

    def fake_get_default_indicator_provider():
        return sentinel_provider

    def fake_get_latest_atr(symbol, indicator_provider, period=14, interval="1d"):
        captured["symbol"] = symbol
        captured["indicator_provider"] = indicator_provider
        captured["period"] = period
        return 2.5  # a usable ATR -> non-zero sizing

    # _auto_size_by_risk imports both lazily: get_default_indicator_provider from
    # .seam_helpers and get_latest_atr from .position_sizing. Patch on those modules.
    import ba2_trade_platform.core.seam_helpers as sh
    import ba2_trade_platform.core.position_sizing as ps
    monkeypatch.setattr(sh, "get_default_indicator_provider", fake_get_default_indicator_provider)
    monkeypatch.setattr(ps, "get_latest_atr", fake_get_latest_atr)

    tk = _make_toolkit()
    result = tk._auto_size_by_risk("AAPL", OrderDirection.BUY, sl_price=None)

    assert captured["indicator_provider"] is sentinel_provider, (
        "ATR fetch must receive the injected host indicator provider, not None/missing"
    )
    assert captured["symbol"] == "AAPL"
    assert captured["period"] == 14
    # ATR=2.5, atr_mult=2 -> risk/share=5 on a $100k equity at 1% risk ($1000)
    # -> 200 shares, capped by max_position_value ($10k -> 100 shares).
    assert result["quantity"] > 0


def test_auto_size_with_sl_skips_atr_fetch(monkeypatch):
    """When an SL price is given, the ATR path is skipped entirely: neither the
    provider builder nor get_latest_atr should be invoked."""
    import ba2_trade_platform.core.seam_helpers as sh
    import ba2_trade_platform.core.position_sizing as ps

    def boom_provider():
        raise AssertionError("indicator provider must not be built when SL is given")

    def boom_atr(*a, **k):
        raise AssertionError("get_latest_atr must not be called when SL is given")

    monkeypatch.setattr(sh, "get_default_indicator_provider", boom_provider)
    monkeypatch.setattr(ps, "get_latest_atr", boom_atr)

    tk = _make_toolkit()
    result = tk._auto_size_by_risk("AAPL", OrderDirection.BUY, sl_price=95.0)
    assert "quantity" in result


def test_auto_size_provider_build_failure_degrades_to_none(monkeypatch):
    """A provider-build failure must not crash sizing: it degrades to a None
    provider (get_latest_atr returns None), which the pure math handles."""
    import ba2_trade_platform.core.seam_helpers as sh
    import ba2_trade_platform.core.position_sizing as ps

    def failing_provider():
        raise RuntimeError("no network")

    captured = {}

    def fake_get_latest_atr(symbol, indicator_provider, period=14, interval="1d"):
        captured["indicator_provider"] = indicator_provider
        return None

    monkeypatch.setattr(sh, "get_default_indicator_provider", failing_provider)
    monkeypatch.setattr(ps, "get_latest_atr", fake_get_latest_atr)

    tk = _make_toolkit()
    result = tk._auto_size_by_risk("AAPL", OrderDirection.BUY, sl_price=None)
    assert captured["indicator_provider"] is None
    assert "quantity" in result
