"""Tests for the TradingAgentsUI 'Data provided to this analyst' expander."""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from ba2_trade_platform.modules.experts.TradingAgentsUI import TradingAgentsUI
from ba2_trade_platform.core.types import MarketAnalysisStatus


def _make_market_analysis(status):
    ma = MagicMock()
    ma.status = status
    ma.state = {"trading_agent_graph": {"market_report": "report", "market_input": "system prompt + data"}}
    return ma


def _render_with_patches(ui_obj, render_method_name):
    """Patch nicegui `ui` and every sub-render panel to no-ops, then invoke the
    given top-level render method. Returns the Mock that replaced
    ``_render_input_expander`` so callers can assert how it was called."""
    sub_methods = [
        "_render_content_panel",
        "_render_data_visualization_panel",
        "_render_tool_outputs_panel",
        "_render_summary_panel",
        "_render_in_progress_summary",
        "_render_debate_panel",
        "_render_expert_recommendation",
    ]
    with ExitStack() as stack:
        stack.enter_context(patch("ba2_trade_platform.modules.experts.TradingAgentsUI.ui", MagicMock()))
        for name in sub_methods:
            stack.enter_context(patch.object(ui_obj, name))
        mock_expander = stack.enter_context(patch.object(ui_obj, "_render_input_expander"))
        getattr(ui_obj, render_method_name)()
    return mock_expander


class TestMarketAnalysisPromptExpander:
    def test_completed_ui_shows_market_input_expander(self):
        ui_obj = TradingAgentsUI(_make_market_analysis(MarketAnalysisStatus.COMPLETED))
        mock_expander = _render_with_patches(ui_obj, "_render_completed_ui")
        mock_expander.assert_any_call("market_input")

    def test_in_progress_ui_shows_market_input_expander(self):
        ui_obj = TradingAgentsUI(_make_market_analysis(MarketAnalysisStatus.RUNNING))
        mock_expander = _render_with_patches(ui_obj, "_render_in_progress_ui")
        mock_expander.assert_any_call("market_input")
