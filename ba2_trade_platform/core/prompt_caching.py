"""
Prompt caching helpers.

Prompt caching is configured at the LLM-client layer, not by LangGraph. The two
providers we use behave differently:

* **OpenAI** (incl. gpt5.x): caches long prompt prefixes *automatically* — no code
  change is needed beyond keeping the stable content (system prompt, injected data)
  at the front of the message list. These helpers are a no-op for OpenAI.

* **Anthropic** (Claude): requires explicit ``cache_control`` breakpoints. The system
  prompt (and any large, reused content block) must be marked with
  ``{"type": "ephemeral"}`` so the prefix up to that point is cached (~5 min TTL).

``apply_anthropic_prompt_caching`` rewrites the first ``SystemMessage`` of a message
list into Anthropic block form with a cache breakpoint, but ONLY when the bound LLM
is a ``ChatAnthropic`` instance. For every other provider it returns the messages
untouched, so call sites can apply it unconditionally.
"""

from typing import Any, List

from ..logger import logger


def is_anthropic_llm(llm: Any) -> bool:
    """Return True if ``llm`` is a langchain ChatAnthropic instance.

    Detected by class name to avoid importing langchain_anthropic (which may not be
    installed in every environment).
    """
    for klass in type(llm).__mro__:
        if klass.__name__ == "ChatAnthropic":
            return True
    return False


def _text_to_cached_blocks(text: str) -> list:
    """Wrap plain text in an Anthropic content-block list with a cache breakpoint."""
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def apply_anthropic_prompt_caching(messages: List[Any], llm: Any) -> List[Any]:
    """Add an Anthropic ``cache_control`` breakpoint to the system prompt.

    When ``llm`` is a ChatAnthropic model, the first ``SystemMessage`` whose content is
    a plain string is rewritten into block form with an ephemeral cache breakpoint, so
    the (large, stable) system prompt prefix is cached and reused across calls. For all
    other providers the list is returned unchanged.

    The input list is not mutated; a shallow copy is returned when a change is made.
    """
    if not is_anthropic_llm(llm):
        return messages

    from langchain_core.messages import SystemMessage

    new_messages = list(messages)
    for idx, msg in enumerate(new_messages):
        if isinstance(msg, SystemMessage) and isinstance(msg.content, str):
            cached = SystemMessage(content=_text_to_cached_blocks(msg.content))
            # Preserve any additional kwargs (e.g. name) the original carried.
            new_messages[idx] = cached
            logger.debug("Applied Anthropic ephemeral cache_control to system prompt")
            break  # Only the first system message needs the breakpoint.

    return new_messages


def extract_cache_usage(usage_metadata: Any) -> dict:
    """Pull cache token counts out of a LangChain ``usage_metadata`` dict.

    LangChain normalizes provider usage into ``usage_metadata`` with an
    ``input_token_details`` sub-dict. Anthropic reports ``cache_read`` and
    ``cache_creation``; OpenAI reports ``cache_read`` (its "cached_tokens").

    Returns a dict with ``cache_read``, ``cache_creation``, and ``input_tokens`` ints
    (zeros when absent). Safe against ``None``/malformed input.
    """
    result = {"cache_read": 0, "cache_creation": 0, "input_tokens": 0}
    if not isinstance(usage_metadata, dict):
        return result

    result["input_tokens"] = int(usage_metadata.get("input_tokens", 0) or 0)

    details = usage_metadata.get("input_token_details") or {}
    if isinstance(details, dict):
        result["cache_read"] = int(details.get("cache_read", 0) or 0)
        result["cache_creation"] = int(details.get("cache_creation", 0) or 0)

    return result
