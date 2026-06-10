"""Guards against the duplicate-prompt-definition bug (PR-1) and stale tool
references (PR-2) in the TradingAgents prompt registry.

Background: SIGNAL_PROCESSING_SYSTEM_PROMPT and REFLECTION_SYSTEM_PROMPT used to
be defined twice; Python kept the second (weaker) binding, so the registry
silently served the wrong prompts. These tests fail if that regresses.
"""
import ast
import re
from pathlib import Path

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents import prompts as prompts_mod
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.prompts import (
    PROMPT_REGISTRY,
    NO_PAST_MEMORIES_TEXT,
    format_past_memories,
    get_prompt,
)

PROMPTS_SRC = Path(prompts_mod.__file__).read_text()


def _module_level_assignment_count(name: str) -> int:
    """Count top-level `NAME = ...` assignments in prompts.py via AST."""
    tree = ast.parse(PROMPTS_SRC)
    count = 0
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    count += 1
    return count


class TestNoDuplicatePromptDefinitions:
    def test_signal_processing_defined_once(self):
        assert _module_level_assignment_count("SIGNAL_PROCESSING_SYSTEM_PROMPT") == 1

    def test_reflection_defined_once(self):
        assert _module_level_assignment_count("REFLECTION_SYSTEM_PROMPT") == 1


class TestRegistryServesIntendedPrompts:
    def test_signal_processing_prompt_is_strict(self):
        # The strict extractor must instruct single-word output.
        assert "Output only the single rating word" in get_prompt("signal_processing")

    def test_reflection_prompt_is_detailed(self):
        # The detailed 5-section reflection prompt, not the one-liner.
        reflection = get_prompt("reflection")
        assert "Performance Analysis" in reflection
        assert "Actionable Recommendations" in reflection

    def test_all_registry_values_are_nonempty_strings(self):
        for name, value in PROMPT_REGISTRY.items():
            assert isinstance(value, str) and value.strip(), f"empty prompt: {name}"


class TestPastMemoriesFormatting:
    def test_empty_returns_no_memories_sentinel(self):
        assert format_past_memories([]) == NO_PAST_MEMORIES_TEXT
        assert format_past_memories(None) == NO_PAST_MEMORIES_TEXT

    def test_joins_recommendations(self):
        out = format_past_memories([{"recommendation": "A"}, {"recommendation": "B"}])
        assert "A" in out and "B" in out
        assert out != NO_PAST_MEMORIES_TEXT


class TestResearchManagerScale:
    def test_uses_five_tier_scale(self):
        rm = get_prompt("research_manager")
        for tier in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
            assert tier in rm


class TestFinalSummarizationNoDuplicateSchema:
    def test_schema_owned_by_format_instructions(self):
        fs = get_prompt("final_summarization")
        assert "JSON SCHEMA" not in fs
        assert "DECISION FRAMEWORK" in fs  # decision-framework content retained


class TestMarketAnalystToolReferences:
    def test_no_stale_get_yfin_data_reference(self):
        assert "get_YFin_data" not in get_prompt("market_analyst")

    def test_references_actual_tools(self):
        market = get_prompt("market_analyst")
        assert "get_ohlcv_data" in market
        assert "get_indicator_data" in market

    def test_no_unavailable_indicator_examples(self):
        # stochrsi is not in the offered indicator list.
        assert "stochrsi" not in get_prompt("market_analyst")
