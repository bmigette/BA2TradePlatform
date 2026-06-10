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
