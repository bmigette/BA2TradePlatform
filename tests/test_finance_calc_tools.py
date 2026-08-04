"""Agentic compute tools: presence in the right tool lists + validation behavior."""
import pytest


def _tool_nodes():
    """Build the graph's tool nodes with a stubbed toolkit (no provider calls)."""
    from unittest.mock import MagicMock
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
    g = TradingAgentsGraph.__new__(TradingAgentsGraph)
    g.ticker = "AAA"
    g.toolkit = MagicMock()
    g.provider_args = {}
    g.market_analysis_id = None
    return g._create_tool_nodes()


def test_compute_tools_in_correct_lists():
    nodes = _tool_nodes()
    market = set(nodes["market"].original_tools)
    fund = set(nodes["fundamentals"].original_tools)
    assert {"compute_derivatives_black_scholes", "compute_arithmetic"} <= market
    assert {"compute_valuation_wacc", "compute_valuation_dcf",
            "compute_valuation_dcf_sensitivity", "compute_fixed_income_bond",
            "compute_arithmetic"} <= fund
    # The judgment-input valuation tools must NOT leak into the market list.
    assert "compute_valuation_dcf" not in market
    # Series-based risk/stats tools exist NOWHERE (they are provider-injected).
    assert not any(n.startswith("compute_risk") for n in market | fund)


def _get(nodes, role, name):
    return nodes[role].original_tools[name]


def test_compute_arithmetic_tool():
    nodes = _tool_nodes()
    out = _get(nodes, "fundamentals", "compute_arithmetic").invoke({"expression": "(37-13)/13*100"})
    assert "184.615" in out


def test_compute_dcf_tool_and_validation_error():
    nodes = _tool_nodes()
    dcf = _get(nodes, "fundamentals", "compute_valuation_dcf")
    out = dcf.invoke({"fcf_schedule": [100.0, 110.0, 121.0], "discount_rate": 0.10,
                      "terminal_method": "gordon_growth", "terminal_growth_rate": 0.02,
                      "net_debt": 100.0, "shares_outstanding": 100.0})
    assert "Intrinsic value/share" in out
    bad = dcf.invoke({"fcf_schedule": [100.0], "discount_rate": 0.10,
                      "terminal_method": "gordon_growth", "terminal_growth_rate": 0.10})
    assert bad.startswith("Error:")          # g >= r rejected, string not exception
    assert "terminal_growth_rate" in bad


def test_black_scholes_tool():
    nodes = _tool_nodes()
    out = _get(nodes, "market", "compute_derivatives_black_scholes").invoke(
        {"spot": 100.0, "strike": 100.0, "years": 1.0, "rate": 0.05, "vol": 0.2,
         "option_type": "call"})
    assert "Black-Scholes call" in out and "10.45" in out


class _FakeLLM:
    """Minimal bind_tools/invoke fake — no provider, no API."""

    def __init__(self, tool_calls=None):
        self._tool_calls = tool_calls or []
        self.bound = None

    def bind_tools(self, tools, **kwargs):
        self.bound = [t.name for t in tools]
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage
        return AIMessage(content="fundamentals report text", tool_calls=self._tool_calls)


def _node_state():
    return {"trade_date": "2026-01-15", "company_of_interest": "AAA", "messages": []}


def test_fundamentals_hybrid_reports_without_tool_calls():
    from unittest.mock import MagicMock, patch
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst import (
        create_fundamentals_analyst)
    llm = _FakeLLM()
    tool = MagicMock()
    tool.name = "compute_valuation_dcf"
    node = create_fundamentals_analyst(llm, MagicMock(), [tool])
    with patch("ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst.gather_fundamentals_context",
               return_value="stub context"):
        out = node(_node_state())
    assert llm.bound == ["compute_valuation_dcf"]      # tools are actually bound
    assert out["fundamentals_report"] == "fundamentals report text"
    assert "stub context" in out["fundamentals_input"]


def test_fundamentals_hybrid_defers_report_on_tool_calls():
    from unittest.mock import MagicMock, patch
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst import (
        create_fundamentals_analyst)
    llm = _FakeLLM(tool_calls=[{"name": "compute_valuation_dcf", "args": {}, "id": "1"}])
    node = create_fundamentals_analyst(llm, MagicMock(), [])
    with patch("ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst.gather_fundamentals_context",
               return_value="stub context"):
        out = node(_node_state())
    assert out["fundamentals_report"] == ""            # graph routes to tools_fundamentals
    assert out["messages"]                             # the AIMessage goes back into state


class _RecordingLLM:
    """Fake LLM that captures the exact message payload of every invoke."""

    def __init__(self, tool_calls=None):
        self._tool_calls = tool_calls or []
        self.invocations = []

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage
        self.invocations.append(list(messages))
        return AIMessage(content="fundamentals report text", tool_calls=self._tool_calls)


def test_fundamentals_hybrid_reentry_sees_tool_messages_and_skips_regather():
    """Two-turn ReAct flow: after tools_fundamentals runs, the node is re-entered
    with an AIMessage(tool_calls) + ToolMessage in state. The re-entry must (a)
    pass those messages to llm.invoke (otherwise the LLM never sees the tool
    results and loops until the recursion limit) and (b) NOT re-gather the
    fundamentals context (no duplicate provider calls)."""
    from unittest.mock import MagicMock, patch
    from langchain_core.messages import AIMessage, ToolMessage
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst import (
        create_fundamentals_analyst)

    llm = _RecordingLLM()
    node = create_fundamentals_analyst(llm, MagicMock(), [])
    gather = MagicMock(return_value="stub context")
    with patch("ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst.gather_fundamentals_context",
               gather):
        # Turn 1: first entry — gathers context, LLM requests a tool call.
        out1 = node(_node_state())
        # The AIMessage the first turn put into state (with its tool call) ...
        ai_msg = AIMessage(
            content="",
            tool_calls=[{"name": "compute_valuation_dcf", "args": {}, "id": "1"}])
        # ... plus the ToolMessage tools_fundamentals appended.
        tool_msg = ToolMessage(content="Intrinsic value/share: $13.32",
                               tool_call_id="1")
        state2 = {**_node_state(),
                  "messages": [ai_msg, tool_msg],
                  "fundamentals_input": out1["fundamentals_input"]}
        # Turn 2: re-entry.
        out2 = node(state2)

    assert gather.call_count == 1                      # no re-gather on re-entry
    assert len(llm.invocations) == 2
    second_payload = llm.invocations[1]
    assert any(getattr(m, "type", None) == "tool" for m in second_payload), \
        "re-entry payload must include the ToolMessage"
    assert ai_msg in second_payload, \
        "re-entry payload must include the AIMessage with the tool calls"
    assert out2["fundamentals_report"] == "fundamentals report text"


def test_fundamentals_hybrid_reentry_without_marker_regathers():
    """Legacy/foreign state without the snapshot marker: fall back to
    re-gathering the context rather than crashing."""
    from unittest.mock import MagicMock, patch
    from langchain_core.messages import AIMessage, ToolMessage
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst import (
        create_fundamentals_analyst)

    llm = _RecordingLLM()
    node = create_fundamentals_analyst(llm, MagicMock(), [])
    gather = MagicMock(return_value="stub context")
    ai_msg = AIMessage(content="",
                       tool_calls=[{"name": "compute_arithmetic", "args": {}, "id": "1"}])
    tool_msg = ToolMessage(content="42", tool_call_id="1")
    state = {**_node_state(), "messages": [ai_msg, tool_msg],
             "fundamentals_input": "no marker here"}
    with patch("ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst.gather_fundamentals_context",
               gather):
        node(state)
    assert gather.call_count == 1
    assert any(getattr(m, "type", None) == "tool" for m in llm.invocations[0])


def test_compute_arithmetic_error_paths_return_error_string():
    """ZeroDivisionError / SyntaxError / OverflowError must surface as 'Error:'
    strings, never escape the tool node."""
    nodes = _tool_nodes()
    arith = _get(nodes, "fundamentals", "compute_arithmetic")
    for bad in ("1/0", "(((", "10**1000 * 1.0"):
        out = arith.invoke({"expression": bad})
        assert out.startswith("Error:"), f"{bad!r} -> {out!r}"
