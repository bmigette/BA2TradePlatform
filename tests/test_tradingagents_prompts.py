"""Tests for strategy-notes injection into TradingAgents synthesis/execution prompts."""
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.prompts import (
    format_research_manager_prompt,
    format_risk_manager_prompt,
    format_trader_system_prompt,
)


class TestStrategyNotesInjection:
    def test_research_manager_prompt_includes_notes(self):
        result = format_research_manager_prompt(
            strategy_notes="BUY THE DIP STRATEGY", past_memory_str="PM", history="H",
        )
        assert "BUY THE DIP STRATEGY" in result

    def test_research_manager_prompt_omits_notes_section_when_empty(self):
        result = format_research_manager_prompt(strategy_notes="", past_memory_str="PM", history="H")
        assert "Strategy Context" not in result

    def test_risk_manager_prompt_includes_notes(self):
        result = format_risk_manager_prompt(
            strategy_notes="BUY THE DIP STRATEGY", trader_plan="PLAN", past_memory_str="PM", history="H",
        )
        assert "BUY THE DIP STRATEGY" in result

    def test_trader_system_prompt_includes_notes(self):
        result = format_trader_system_prompt(past_memory_str="PM", strategy_notes="BUY THE DIP STRATEGY")
        assert "BUY THE DIP STRATEGY" in result

    def test_trader_system_prompt_defaults_to_no_notes(self):
        result = format_trader_system_prompt(past_memory_str="PM")
        assert "Strategy Context" not in result
