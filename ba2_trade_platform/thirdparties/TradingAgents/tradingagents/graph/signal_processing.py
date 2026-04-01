# TradingAgents/graph/signal_processing.py

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
import time
import json
from ..prompts import get_prompt
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response


class SignalProcessor:
    """Processes trading signals to extract actionable decisions."""

    def __init__(self, quick_thinking_llm: ChatOpenAI):
        """Initialize with an LLM for processing."""
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """
        Process a full trading signal to extract the core decision.

        Args:
            full_signal: Complete trading signal text

        Returns:
            Extracted decision (BUY, OVERWEIGHT, HOLD, UNDERWEIGHT, or SELL)
        """
        messages = [
            (
                "system",
                get_prompt("signal_processing"),
            ),
            ("human", full_signal),
        ]

        return extract_text_from_llm_response(self.quick_thinking_llm.invoke(messages).content)
