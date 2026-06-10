"""Tests for new TradingAgents memory-scope and strategy-notes settings."""
import pytest

from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents
from ba2_trade_platform.core.types import AnalysisUseCase
from tests.factories import create_account_definition, create_expert_instance


class TestNewSettingsDefinitions:
    def test_memory_injection_scope_definition(self):
        defs = TradingAgents.get_settings_definitions()
        d = defs["memory_injection_scope"]
        assert d["type"] == "str"
        assert d["default"] == "same_symbol"
        assert d["valid_values"] == ["none", "same_symbol", "all_symbols"]

    def test_memory_max_trades_definition(self):
        defs = TradingAgents.get_settings_definitions()
        d = defs["memory_max_trades"]
        assert d["type"] == "int"
        assert d["default"] == 2

    def test_memory_lookback_days_definition(self):
        defs = TradingAgents.get_settings_definitions()
        d = defs["memory_lookback_days"]
        assert d["type"] == "int"
        assert d["default"] == 14

    def test_analysis_strategy_notes_definition(self):
        defs = TradingAgents.get_settings_definitions()
        d = defs["analysis_strategy_notes"]
        assert d["type"] == "str"
        assert d["required"] is False
        assert d["default"] == ""


@pytest.fixture
def ta_expert(db_session):
    account = create_account_definition()
    instance = create_expert_instance(account.id, expert="TradingAgents")
    return TradingAgents(instance.id)


class TestCreateConfigMemorySettings:
    def test_defaults_propagate_to_config(self, ta_expert):
        config = ta_expert._create_tradingagents_config(AnalysisUseCase.ENTER_MARKET)
        assert config["memory_injection_scope"] == "same_symbol"
        assert config["memory_max_trades"] == 2
        assert config["memory_lookback_days"] == 14
        assert config["analysis_strategy_notes"] == ""

    def test_overrides_propagate_to_config(self, ta_expert, db_session):
        # int- and str-typed settings are both read from the value_str column by
        # ExtendableSettingsInterface.settings (there is no dedicated int branch),
        # so store every override as value_str.
        from ba2_trade_platform.core.models import ExpertSetting
        from ba2_trade_platform.core.db import add_instance
        add_instance(ExpertSetting(instance_id=ta_expert.instance.id, key="memory_injection_scope", value_str="all_symbols"))
        add_instance(ExpertSetting(instance_id=ta_expert.instance.id, key="memory_max_trades", value_str="5"))
        add_instance(ExpertSetting(instance_id=ta_expert.instance.id, key="memory_lookback_days", value_str="30"))
        add_instance(ExpertSetting(instance_id=ta_expert.instance.id, key="analysis_strategy_notes", value_str="Buy the dip on broken charts."))

        # settings cache is populated lazily on first access; a fresh instance reloads
        ta_expert2 = TradingAgents(ta_expert.instance.id)
        config = ta_expert2._create_tradingagents_config(AnalysisUseCase.ENTER_MARKET)
        assert config["memory_injection_scope"] == "all_symbols"
        assert config["memory_max_trades"] == 5
        assert config["memory_lookback_days"] == 30
        assert config["analysis_strategy_notes"] == "Buy the dip on broken charts."
