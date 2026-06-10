"""Tests that the Market Analyst exposes its system prompt via market_input,
matching the pattern used by the other analysts (macro/news/social/fundamentals)."""
from unittest.mock import MagicMock, patch


def test_market_analyst_returns_market_input():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.market_analyst import (
        create_market_analyst,
    )

    llm = MagicMock()
    result_mock = MagicMock()
    result_mock.tool_calls = []
    result_mock.content = "Market report text"

    with patch(
        "ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.market_analyst.ChatPromptTemplate"
    ) as mock_template_cls:
        prompt_mock = MagicMock()
        mock_template_cls.from_messages.return_value = prompt_mock
        chain_mock = prompt_mock.__or__.return_value
        chain_mock.invoke.return_value = result_mock

        node = create_market_analyst(llm, toolkit=None, tools=[])
        state = {"trade_date": "2026-06-10", "company_of_interest": "AAPL", "messages": []}
        out = node(state)

    assert "market_input" in out
    assert out["market_input"].strip()
    assert "trading assistant" in out["market_input"].lower()  # from MARKET_ANALYST_SYSTEM_PROMPT
