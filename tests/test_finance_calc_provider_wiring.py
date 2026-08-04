"""Wiring: registries, expert settings, provider_map, toolkit methods."""
import pytest

from ba2_trade_platform.core.interfaces import RiskStatsInterface, ValuationSnapshotInterface
from tests.factories import create_account_definition, create_expert_instance


def test_registries_contain_finance_calc():
    from ba2_providers import RISK_STATS_PROVIDERS, VALUATION_PROVIDERS
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    from ba2_providers.valuation import FinanceCalcValuationProvider
    assert RISK_STATS_PROVIDERS["finance_calc"] is FinanceCalcRiskStatsProvider
    assert VALUATION_PROVIDERS["finance_calc"] is FinanceCalcValuationProvider


def test_live_shim_reexports_registries():
    from ba2_trade_platform.modules import dataproviders
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    from ba2_providers.valuation import FinanceCalcValuationProvider
    assert dataproviders.RISK_STATS_PROVIDERS["finance_calc"] is FinanceCalcRiskStatsProvider
    assert dataproviders.VALUATION_PROVIDERS["finance_calc"] is FinanceCalcValuationProvider


def test_expert_settings_and_provider_map(db_session):
    from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents
    defs = TradingAgents.get_settings_definitions()
    assert defs["vendor_risk_stats"]["default"] == ["finance_calc"]
    assert defs["vendor_valuation"]["default"] == ["finance_calc"]

    from ba2_trade_platform.core.models import ExpertSetting
    from ba2_trade_platform.core.db import add_instance
    account = create_account_definition()
    instance = create_expert_instance(account.id, expert="TradingAgents")
    # Fresh instances have no DB rows and ExtendableSettingsInterface.settings
    # yields None (not the definition default) for unset keys, which
    # _build_provider_map treats as an empty vendor list — so seed the list
    # settings explicitly (list settings are stored as value_json).
    add_instance(ExpertSetting(instance_id=instance.id, key="vendor_risk_stats", value_json=["finance_calc"]))
    add_instance(ExpertSetting(instance_id=instance.id, key="vendor_valuation", value_json=["finance_calc"]))
    expert = TradingAgents(instance.id)
    provider_map = expert._build_provider_map()
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    from ba2_providers.valuation import FinanceCalcValuationProvider
    assert FinanceCalcRiskStatsProvider in provider_map["risk_stats"]
    assert FinanceCalcValuationProvider in provider_map["valuation"]


def test_toolkit_has_compute_methods():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils_new import Toolkit
    assert callable(getattr(Toolkit, "get_risk_stats", None))
    assert callable(getattr(Toolkit, "get_valuation_snapshot", None))


# ---------------------------------------------------------------------------
# Toolkit fallback-loop behaviour (stub compute providers, no network)
# ---------------------------------------------------------------------------

class _FakeOhlcvProvider:
    """Minimal stand-in for an OHLCV provider (composition only)."""

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d"):
        raise NotImplementedError


class _StubRiskStatsProvider(RiskStatsInterface):
    def __init__(self, ohlcv_provider, benchmark_symbol: str = "SPY"):
        self._ohlcv = ohlcv_provider

    def get_provider_name(self) -> str:
        return "stub_risk"

    def get_supported_features(self):
        return ["risk_stats"]

    def validate_config(self) -> bool:
        return True

    def _format_as_dict(self, data):
        return data

    def _format_as_markdown(self, data):
        return str(data)

    def get_risk_stats(self, symbol, end_date, lookback_days=365, format_type="markdown"):
        data = {"symbol": symbol, "stub": True, "lookback_days": lookback_days}
        if format_type == "both":
            return {"text": f"# Risk statistics — {symbol} (stub)", "data": data}
        if format_type == "dict":
            return data
        return f"# Risk statistics — {symbol} (stub)"


class _FailingRiskStatsProvider(_StubRiskStatsProvider):
    def get_risk_stats(self, symbol, end_date, lookback_days=365, format_type="markdown"):
        raise RuntimeError("stub boom")


class _StubValuationProvider(ValuationSnapshotInterface):
    def __init__(self, fundamentals_overview_provider, fundamentals_details_provider,
                 ohlcv_provider=None):
        self._overview = fundamentals_overview_provider
        self._details = fundamentals_details_provider
        self._ohlcv = ohlcv_provider

    def get_provider_name(self) -> str:
        return "stub_valuation"

    def get_supported_features(self):
        return ["valuation_snapshot"]

    def validate_config(self) -> bool:
        return True

    def _format_as_dict(self, data):
        return data

    def _format_as_markdown(self, data):
        return str(data)

    def get_valuation_snapshot(self, symbol, as_of_date, format_type="markdown"):
        data = {"symbol": symbol, "stub": True}
        if format_type == "both":
            return {"text": f"# Valuation snapshot — {symbol} (stub)", "data": data}
        if format_type == "dict":
            return data
        return f"# Valuation snapshot — {symbol} (stub)"


class _StubOverviewProvider:
    pass


class _StubDetailsProvider:
    pass


def _make_toolkit(provider_map):
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils_new import Toolkit
    return Toolkit(provider_map=provider_map)


def test_toolkit_get_risk_stats_fallback_and_storage():
    toolkit = _make_toolkit({
        "ohlcv": [_FakeOhlcvProvider],
        "risk_stats": [_FailingRiskStatsProvider, _StubRiskStatsProvider],
    })
    result = toolkit.get_risk_stats("AAPL", end_date="2025-06-30", lookback_days=365)
    assert result["_internal"] is True
    assert "AAPL" in result["text_for_agent"]
    storage = result["json_for_storage"]
    assert storage["tool"] == "get_risk_stats"
    assert storage["symbol"] == "AAPL"
    assert storage["end_date"] == "2025-06-30"
    assert storage["provider"] == "_StubRiskStatsProvider"
    assert storage["data"]["stub"] is True


def test_toolkit_get_risk_stats_all_providers_failed():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils_new import (
        AllProvidersFailedError,
    )
    toolkit = _make_toolkit({
        "ohlcv": [_FakeOhlcvProvider],
        "risk_stats": [_FailingRiskStatsProvider],
    })
    with pytest.raises(AllProvidersFailedError):
        toolkit.get_risk_stats("AAPL", end_date="2025-06-30")


def test_toolkit_get_valuation_snapshot_success():
    toolkit = _make_toolkit({
        "ohlcv": [_FakeOhlcvProvider],
        "fundamentals_overview": [_StubOverviewProvider],
        "fundamentals_details": [_StubDetailsProvider],
        "valuation": [_StubValuationProvider],
    })
    result = toolkit.get_valuation_snapshot("MSFT", as_of_date="2025-06-30")
    assert result["_internal"] is True
    assert "MSFT" in result["text_for_agent"]
    storage = result["json_for_storage"]
    assert storage["tool"] == "get_valuation_snapshot"
    assert storage["symbol"] == "MSFT"
    assert storage["data"]["stub"] is True
