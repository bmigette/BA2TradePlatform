"""ExpertDataExportInterface: bypass-construction, settings merge, error capture."""
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pandas as pd

from ba2_common.core.interfaces import MarketExpertInterface
# The package's __init__.py re-exports the CLASS under the same name
# (`from .MarketExpertInterface import MarketExpertInterface`), which shadows
# the submodule as an attribute — so `get_instance` (a name bound inside that
# submodule, imported via `from ba2_common.core.db import get_instance`) must
# be patched via sys.modules, not via attribute access off the package/class.
_mei_module = sys.modules["ba2_common.core.interfaces.MarketExpertInterface"]
from ba2_common.core.interfaces.ExpertDataExportInterface import (
    ExpertDataExport, ExpertDataExportInterface, ExpertMetric,
)
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.types import OrderRecommendation, Recommendation


class _FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [42.0]})


def _resolver(cat, name, **kw):
    return {"ohlcv": _FakeOHLCV()}[cat]


class _DummyExpert(ExpertDataExportInterface, MarketExpertInterface):
    """Minimal concrete expert for interface-level tests only."""

    @classmethod
    def description(cls) -> str:
        return "dummy"

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        raise NotImplementedError

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {"threshold": {"type": "float", "required": False, "default": 0.5,
                              "description": "test threshold"},
                "force_skip": {"type": "bool", "required": False, "default": False,
                              "description": "test-only: force a skip=True Recommendation"}}

    def _gather(self, providers, as_of):
        return {"current_price": providers.ohlcv().get_ohlcv_data("X").iloc[-1]["Close"]}

    def _process(self, data_bundle, settings, as_of=None):
        if settings.get("force_skip"):
            return Recommendation(signal=OrderRecommendation.HOLD, confidence=0.0,
                                  current_price=data_bundle["current_price"],
                                  details="", skip=True, skip_reason="no analyst coverage")
        signal = OrderRecommendation.BUY if settings["threshold"] < 1.0 else OrderRecommendation.HOLD
        return Recommendation(signal=signal, confidence=80.0,
                              current_price=data_bundle["current_price"],
                              details="dummy", raw_outputs={"threshold_used": settings["threshold"]})

    def analyze_as_of(self, as_of, context: BacktestContext) -> Recommendation:
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)


class _BasketExpert(_DummyExpert):
    """Simulates a basket expert whose analyze_as_of returns a list of
    Recommendations rather than a single one — export_symbol_data is not
    defined for these and must capture the mismatch as an error."""

    def analyze_as_of(self, as_of, context: BacktestContext):
        return [super().analyze_as_of(as_of, context)]


class _LivePriceExpert(_DummyExpert):
    """_gather's live branch (as_of is None) calls self._get_current_price,
    exactly like FMPRating/FMPInsiderClusterBuy's real live branch does. The
    base MarketExpertInterface._get_current_price is DB-bound (needs a real
    ExpertInstance row for self.id) and silently returns None for the
    sentinel id=-1 bypass instance -- fatal for any expert that then formats
    that price (e.g. FMPRating's unguarded f"${current_price:.2f}")."""

    def _gather(self, providers, as_of):
        price = (self._get_current_price("AAPL") if as_of is None
                 else providers.price_at_date("AAPL", as_of))
        return {"current_price": price}

    def _process(self, data_bundle, settings, as_of=None):
        return Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0,
                              current_price=data_bundle["current_price"],
                              details="live-price-test",
                              raw_outputs={"current_price": data_bundle["current_price"]})


def test_export_symbol_data_uses_defaults_with_no_db():
    result = _DummyExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert isinstance(result, ExpertDataExport)
    assert result.error is None
    assert result.overall_signal == "buy"
    assert result.settings_used["threshold"] == 0.5
    assert result.raw["threshold_used"] == 0.5


def test_export_symbol_data_applies_overrides():
    result = _DummyExpert.export_symbol_data(
        "AAPL", overrides={"threshold": 2.0}, providers_resolver=_resolver)
    assert result.overall_signal == "hold"
    assert result.settings_used["threshold"] == 2.0


def test_export_symbol_data_never_touches_db(monkeypatch):
    """The bypass factory must not require a real ExpertInstance row.

    Proves it by making the DB-instance lookup ``_load_expert_instance`` would
    call fail loudly if invoked, and asserting it's never called — not just
    that no error surfaced (which could equally mean "no DB configured").
    """
    boom = MagicMock(side_effect=AssertionError(
        "get_instance() must never be called by export_symbol_data"))
    monkeypatch.setattr(_mei_module, "get_instance", boom)
    result = _DummyExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert boom.call_count == 0
    assert result.error is None


def test_export_symbol_data_catches_exceptions_into_error():
    def _boom(cat, name, **kw):
        raise RuntimeError("provider exploded")
    result = _DummyExpert.export_symbol_data("AAPL", providers_resolver=_boom)
    assert result.error == "provider exploded"
    assert result.metrics == []


def test_export_symbol_data_skip_sets_skipped_and_preserves_reason():
    result = _DummyExpert.export_symbol_data(
        "AAPL", overrides={"force_skip": True}, providers_resolver=_resolver)
    assert result.error is None
    assert result.skipped is True
    assert len(result.metrics) == 1
    assert result.metrics[0].detail == "no analyst coverage"


def test_export_symbol_data_basket_expert_returns_list_captured_as_error():
    result = _BasketExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is not None
    assert "list" in result.error and "basket" in result.error
    assert result.metrics == []


def test_export_default_settings_includes_builtin_key():
    """Key fact #4 (plan): defaults must include builtin keys, not just the
    expert's own get_settings_definitions() — FactorRanker's single-symbol
    override (a later task) depends on this."""
    defaults = _DummyExpert.export_default_settings()
    assert "enabled_instruments" in defaults
    assert defaults["enabled_instruments"] == {}


def test_export_symbol_data_resolves_live_price_via_providers_when_unoverridden():
    """Reproduces the FMPRating/FMPInsiderClusterBuy bug directly: the live
    _gather branch (as_of is None) calls self._get_current_price(symbol).
    Without the fix this silently resolves to None (the DB-bound base method
    swallows its own lookup failure); with the fix it resolves the injected
    fake OHLCV provider's price (42.0) via providers.price_at_date, exactly
    as the live get_provider("ohlcv", ...) resolver would in production."""
    result = _LivePriceExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is None
    assert result.raw["current_price"] == 42.0


def test_export_symbol_data_respects_class_level_get_current_price_override(monkeypatch):
    """Simulates Task 4/5's test pattern:
    monkeypatch.setattr(SomeExpert, "_get_current_price", lambda self, symbol: ...)
    applied BEFORE calling export_symbol_data. The bypass factory must NOT
    clobber this with its own instance-level price-provider override -- an
    instance attribute always wins over a class attribute in Python's lookup,
    so binding unconditionally would silently ignore the monkeypatch
    regardless of which "happened first" in wall-clock time."""
    monkeypatch.setattr(_LivePriceExpert, "_get_current_price", lambda self, symbol: 99.0)
    result = _LivePriceExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is None
    assert result.raw["current_price"] == 99.0
