"""ExpertDataExportInterface: bypass-construction, settings merge, error capture."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd

from ba2_common.core.interfaces import MarketExpertInterface
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
                              "description": "test threshold"}}

    def _gather(self, providers, as_of):
        return {"current_price": providers.ohlcv().get_ohlcv_data("X").iloc[-1]["Close"]}

    def _process(self, data_bundle, settings, as_of=None):
        signal = OrderRecommendation.BUY if settings["threshold"] < 1.0 else OrderRecommendation.HOLD
        return Recommendation(signal=signal, confidence=80.0,
                              current_price=data_bundle["current_price"],
                              details="dummy", raw_outputs={"threshold_used": settings["threshold"]})

    def analyze_as_of(self, as_of, context: BacktestContext) -> Recommendation:
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)


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


def test_export_symbol_data_never_touches_db():
    """The bypass factory must not require a real ExpertInstance row."""
    # id is a sentinel; if this ever hit the DB with id=-1 it would raise
    # ValueError("ExpertInstance with ID -1 not found") from _load_expert_instance.
    result = _DummyExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is None


def test_export_symbol_data_catches_exceptions_into_error():
    def _boom(cat, name, **kw):
        raise RuntimeError("provider exploded")
    result = _DummyExpert.export_symbol_data("AAPL", providers_resolver=_boom)
    assert result.error == "provider exploded"
    assert result.metrics == []
