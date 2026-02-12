"""
ChatDeepSeek subclass for Moonshot (Kimi) API with thinking mode support.

Kimi's API is OpenAI-compatible but has a special "thinking" mode that returns
`reasoning_content` alongside `content`. During multi-turn tool calls, the
reasoning_content must be preserved in assistant messages, otherwise the API
returns: "thinking is enabled but reasoning_content is missing in assistant
tool call message"

ChatDeepSeek already captures reasoning_content from responses into
additional_kwargs (via _create_chat_result and _convert_chunk_to_generation_chunk),
but doesn't re-inject it into outgoing messages. This subclass adds that.

See: https://platform.moonshot.ai/docs/guide/use-kimi-k2-thinking-model
"""

from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, HumanMessage
from langchain_deepseek import ChatDeepSeek
from pydantic import Field

from ..logger import logger


class ChatKimiThinking(ChatDeepSeek):
    """Moonshot (Kimi) chat model with thinking mode and reasoning_content preservation.

    Subclasses ChatDeepSeek to inherit:
    - reasoning_content capture from API responses (into additional_kwargs)
    - Streaming reasoning_content support
    - Tool content serialization (list -> JSON string)
    - Assistant content normalization (list -> string)
    - Full BaseChatOpenAI benefits (OpenAI SDK client, async, token counting, callbacks)

    Adds:
    - Re-injection of reasoning_content into outgoing assistant messages
    - Thinking mode control (enabled/disabled) via extra_body
    """

    thinking_enabled: bool = True
    api_base: str = Field(default="https://api.moonshot.ai/v1")

    @property
    def _llm_type(self) -> str:
        return "chat-kimi-thinking"

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """Override to re-inject reasoning_content and add thinking parameter.

        Kimi's API requires reasoning_content to be present in assistant messages
        during multi-turn tool call flows. ChatDeepSeek captures it into
        additional_kwargs on the response, but BaseChatOpenAI's message-to-dict
        conversion drops it. We re-inject it here.
        """
        # Resolve original messages to access their additional_kwargs
        if isinstance(input_, str):
            orig_messages = [HumanMessage(content=input_)]
        elif isinstance(input_, list):
            orig_messages = input_
        else:
            orig_messages = input_.to_messages()

        # Get standard payload (ChatDeepSeek handles tool/assistant content formatting)
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # Re-inject reasoning_content into assistant message dicts.
        # Payload messages are converted 1:1 from input messages (no reordering),
        # so we match by counting assistant messages in order.
        reasoning_by_idx: dict[int, str] = {}
        ai_count = 0
        for msg in orig_messages:
            if isinstance(msg, AIMessage):
                rc = msg.additional_kwargs.get("reasoning_content")
                if rc:
                    reasoning_by_idx[ai_count] = rc
                ai_count += 1

        if reasoning_by_idx:
            ai_idx = 0
            for payload_msg in payload.get("messages", []):
                if payload_msg.get("role") == "assistant":
                    if ai_idx in reasoning_by_idx:
                        payload_msg["reasoning_content"] = reasoning_by_idx[ai_idx]
                    ai_idx += 1

        # Add thinking mode control via extra_body
        extra_body = payload.get("extra_body") or {}
        extra_body["thinking"] = {
            "type": "enabled" if self.thinking_enabled else "disabled"
        }
        payload["extra_body"] = extra_body

        return payload
