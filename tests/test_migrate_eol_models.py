"""EOL model migration remap — pure-function tests (no DB)."""
import pytest

from test_tools.migrate_eol_models_2026_08 import remap_value

CASES = {
    # thinking variants keep thinking on the successor
    "moonshot/kimi_k2_thinking": "moonshot/kimi_k2.6",
    "moonshot/kimi_k2_thinking_turbo": "moonshot/kimi_k2.6",
    "moonshot/kimi_k2.5": "moonshot/kimi_k2.6",
    # non-thinking / base variants land on the instant successor
    "moonshot/kimi_k2": "moonshot/kimi_k2.6-nonthinking",
    "moonshot/kimi_k2.5-nonthinking": "moonshot/kimi_k2.6-nonthinking",
    "moonshot/kimi_k1.5": "moonshot/kimi_k2.6-nonthinking",
    # provider-name forms (legacy valid_values format) also remap
    "NagaAI/kimi-k2-thinking": "NagaAI/kimi-k2.6",
    "NagaAI/kimi-k2.5": "NagaAI/kimi-k2.6",
    "NagaAI/moonshot-v1-128k": "NagaAI/kimi-k2.6-nonthinking",
    # deepseek: everything lands on v4-flash (its thinking mode succeeds reasoner)
    "deepseek/deepseek_v3.2": "deepseek/deepseek_v4_flash",
    "deepseek/deepseek_chat": "deepseek/deepseek_v4_flash",
    "deepseek/deepseek_reasoner": "deepseek/deepseek_v4_flash",
    "deepseek/deepseek_coder": "deepseek/deepseek_v4_flash",
    "NagaAI/deepseek-v3.2": "NagaAI/deepseek-v4-flash",
    "NagaAI/deepseek-v3.2:free": "NagaAI/deepseek-v4-flash",
    "NagaAC/deepseek-v3.2-speciale": "NagaAC/deepseek-v4-flash",
    "NagaAI/deepseek-chat-v3.1": "NagaAI/deepseek-v4-flash",
    "NagaAI/deepseek-chat-v3.1:free": "NagaAI/deepseek-v4-flash",
    "NagaAI/deepseek-reasoner-0528": "NagaAI/deepseek-v4-flash",
    "NagaAI/deepseek-reasoner-0528:free": "NagaAI/deepseek-v4-flash",
    "OpenRouter/deepseek/deepseek-chat": "OpenRouter/deepseek/deepseek-v4-flash",
    # xai
    "xai/grok4.1_fast": "xai/grok4.5",
    "xai/grok4.1_fast_reasoning": "xai/grok4.5",
    "NagaAI/grok-4-1-fast-reasoning": "NagaAI/grok-4.5",
    # legacy DB strings use the DOT form of the grok provider name
    "NagaAC/grok-4.1-fast-reasoning": "NagaAC/grok-4.5",
    "NagaAC/grok-4.1-fast-non-reasoning": "NagaAC/grok-4.5",
    # openai o1 family
    "openai/o1": "openai/gpt5.6_terra",
    "OpenRouter/openai/o1-mini": "OpenRouter/openai/gpt-5.6-terra",
    # untouched
    "moonshot/kimi_k2.6": None,
    "openai/gpt5.4": None,
    "NagaAC/gpt-5.1-2025-11-13": None,
    "openai/gpt4o_mini": None,
}


@pytest.mark.parametrize("value,expected", CASES.items())
def test_remap(value, expected):
    assert remap_value(value) == expected
