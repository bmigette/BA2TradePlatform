"""Tests for ba2_trade_platform.core.text_utils."""

from ba2_trade_platform.core.text_utils import extract_text_from_llm_response


def test_plain_string_returned_as_is():
    assert extract_text_from_llm_response("plain text") == "plain text"


def test_gemini_text_blocks_joined():
    content = [
        {"type": "text", "text": "Part 1"},
        {"type": "text", "text": "Part 2"},
    ]
    assert extract_text_from_llm_response(content) == "Part 1\nPart 2"


def test_reasoning_block_is_skipped():
    """GPT-5.x reasoning models prepend an (often empty) reasoning block to the
    content list. It must not leak into the extracted report text."""
    content = [
        {"id": "rs_abc123", "summary": [], "type": "reasoning", "content": []},
        {"type": "text", "text": "## AAPL technical analysis report"},
    ]
    result = extract_text_from_llm_response(content)
    assert result == "## AAPL technical analysis report"
    assert "reasoning" not in result
    assert "rs_abc123" not in result


def test_dict_without_text_is_not_stringified():
    """A non-text block without a 'text' key must not be repr'd into the output."""
    content = [
        {"type": "tool_use", "id": "t1", "name": "foo", "input": {}},
        {"type": "text", "text": "real content"},
    ]
    result = extract_text_from_llm_response(content)
    assert result == "real content"
    assert "tool_use" not in result


def test_all_reasoning_yields_empty_string():
    content = [{"id": "rs_x", "summary": [], "type": "reasoning", "content": []}]
    assert extract_text_from_llm_response(content) == ""


def test_string_list_items_preserved():
    assert extract_text_from_llm_response(["a", "b"]) == "a\nb"
