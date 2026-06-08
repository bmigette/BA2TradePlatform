"""Tests for ba2_trade_platform.core.prompt_caching."""

from langchain_core.messages import SystemMessage, HumanMessage

from ba2_trade_platform.core.prompt_caching import (
    is_anthropic_llm,
    apply_anthropic_prompt_caching,
    extract_cache_usage,
)


class _FakeAnthropic:
    """Stand-in whose class name matches the Anthropic detection."""


_FakeAnthropic.__name__ = "ChatAnthropic"


class _FakeOpenAI:
    pass


_FakeOpenAI.__name__ = "ChatOpenAI"


def test_is_anthropic_detects_by_class_name():
    assert is_anthropic_llm(_FakeAnthropic()) is True
    assert is_anthropic_llm(_FakeOpenAI()) is False


def test_openai_messages_unchanged():
    msgs = [SystemMessage(content="sys"), HumanMessage(content="data")]
    out = apply_anthropic_prompt_caching(msgs, _FakeOpenAI())
    assert out is msgs  # no-op returns the same list
    assert isinstance(out[0].content, str)


def test_anthropic_system_prompt_gets_cache_control():
    msgs = [SystemMessage(content="big system prompt"), HumanMessage(content="data")]
    out = apply_anthropic_prompt_caching(msgs, _FakeAnthropic())

    # Original list not mutated
    assert isinstance(msgs[0].content, str)

    sys_content = out[0].content
    assert isinstance(sys_content, list)
    assert sys_content[0]["type"] == "text"
    assert sys_content[0]["text"] == "big system prompt"
    assert sys_content[0]["cache_control"] == {"type": "ephemeral"}
    # Human message untouched
    assert out[1].content == "data"


def test_only_first_system_message_marked():
    msgs = [
        SystemMessage(content="first"),
        SystemMessage(content="second"),
    ]
    out = apply_anthropic_prompt_caching(msgs, _FakeAnthropic())
    assert isinstance(out[0].content, list)
    assert isinstance(out[1].content, str)  # second left alone


def test_extract_cache_usage_anthropic():
    um = {
        "input_tokens": 1000,
        "output_tokens": 50,
        "input_token_details": {"cache_read": 800, "cache_creation": 120},
    }
    assert extract_cache_usage(um) == {
        "cache_read": 800,
        "cache_creation": 120,
        "input_tokens": 1000,
    }


def test_extract_cache_usage_openai_cached_tokens():
    um = {"input_tokens": 2000, "input_token_details": {"cache_read": 1536}}
    out = extract_cache_usage(um)
    assert out["cache_read"] == 1536
    assert out["cache_creation"] == 0


def test_extract_cache_usage_handles_none():
    assert extract_cache_usage(None) == {
        "cache_read": 0,
        "cache_creation": 0,
        "input_tokens": 0,
    }
