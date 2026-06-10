"""Tests for new TradingAgents memory-scope and strategy-notes settings."""
from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents


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
