"""SYMBOL360's shared expert-card renderer must be able to DISPLAY the evidence.

The reported defect: FMPRating's `(i)` tooltip renders ~1.5 kB of structured
text (analyst buckets, price targets, a 4-step confidence derivation) as ONE
continuous line wider than the viewport, clipped at both ends. Tooltips are
HTML -- newlines collapse to spaces -- and nothing capped the width. It is not
one card's bug: `_render_export_card` is the single renderer behind Earnings /
Insider / FMPRating / FinnHubRating / DeterministicScorer / FactorRanker, so
every one of them was affected.

`nicegui.testing` is used nowhere in this repo; as in tests/test_tradingagents_ui.py
the `ui` module is patched with a MagicMock and the resulting element tree is
asserted from the recorded calls. The WHERE-does-it-go rule itself is pure and
tested in packages/common/tests/test_expert_metric_detail_layout.py.
"""
from unittest.mock import MagicMock, patch

import pytest

from ba2_common.core.interfaces.ExpertDataExportInterface import (
    DETAIL_TOOLTIP_STYLE, ExpertDataExport, ExpertMetric,
)
from ba2_trade_platform.ui.pages import symbol360


_LONG_DETAIL = """Analyst Ratings:
- Strong Buy: 0
- Buy: 7
- Hold: 6
Step 1 - Weighted Scores (Strong Factor: 2.0x):
Buy Score = (Strong Buy x 2.0) + Buy = (0 x 2.0) + 7 = 7.0
Step 2 - Base Confidence = 7.0 / 16.0 x 100 = 43.8%"""


def _render(metrics):
    """Render a one-card export with `metrics` against a recording fake `ui`."""
    export = ExpertDataExport(expert_name="FMPRating", symbol="AAPL",
                              overall_signal="buy", confidence=100.0,
                              metrics=metrics, settings_used={}, relevant_settings=())
    tab = symbol360.Symbol360Tab.__new__(symbol360.Symbol360Tab)
    fake_ui = MagicMock(name="ui")
    with patch.object(symbol360, "ui", fake_ui):
        tab._render_export_card("FMP Rating", export)
    return fake_ui


def _all_text(fake_ui):
    """Every string passed to any ui.<factory>(...) call, positionally."""
    out = []
    for name in ("label", "expansion", "tooltip", "markdown", "html"):
        for call in getattr(fake_ui, name).call_args_list:
            if call.args and isinstance(call.args[0], str):
                out.append(call.args[0])
    return out


def _tooltip_texts(fake_ui):
    """Text handed to a hover tooltip, by EITHER nicegui idiom."""
    texts = [c.args[0] for c in fake_ui.tooltip.call_args_list if c.args]
    texts += [c.args[0] for c in fake_ui.icon.return_value.tooltip.call_args_list if c.args]
    return texts


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_a_long_multi_step_detail_is_not_dumped_into_a_hover_tooltip():
    fake_ui = _render([ExpertMetric("Recommendation", "BUY", "BUY (100%)",
                                    "buy", detail=_LONG_DETAIL)])
    assert _LONG_DETAIL not in _tooltip_texts(fake_ui), (
        "the multi-step derivation is still being rendered as a tooltip, where "
        "its newlines collapse into one unreadable line")


def test_a_long_multi_step_detail_gets_its_own_expandable_panel():
    fake_ui = _render([ExpertMetric("Recommendation", "BUY", "BUY (100%)",
                                    "buy", detail=_LONG_DETAIL)])
    titles = [c.args[0] for c in fake_ui.expansion.call_args_list if c.args]
    assert any("Recommendation" in t for t in titles), titles


def test_the_panel_preserves_the_line_structure_it_was_given():
    """Rendered as steps, one per line -- not run together."""
    fake_ui = _render([ExpertMetric("Recommendation", "BUY", "BUY (100%)",
                                    "buy", detail=_LONG_DETAIL)])
    rendered = _all_text(fake_ui)
    assert any("Step 1 - Weighted Scores" in t and "\n" in t for t in rendered), (
        "no multi-line block was rendered; the steps were flattened")


def test_the_panel_body_asks_the_browser_to_honour_the_newlines():
    """A `\\n` in an HTML text node is whitespace. Without pre-wrap the panel
    would collapse exactly like the tooltip did."""
    fake_ui = _render([ExpertMetric("Recommendation", "BUY", "BUY (100%)",
                                    "buy", detail=_LONG_DETAIL)])
    styles = [c.args[0] for c in fake_ui.label.return_value.style.call_args_list if c.args]
    styles += [c.args[0] for c in fake_ui.label.return_value.classes.return_value.style.call_args_list
               if c.args]
    assert any("pre-wrap" in s for s in styles), styles


# --------------------------------------------------------------------------
# What legitimately stays a tooltip must still be bounded
# --------------------------------------------------------------------------

def test_a_short_detail_stays_a_tooltip():
    fake_ui = _render([ExpertMetric("Analysts", 16, "16", detail="Period: 2026-08-01")])
    assert "Period: 2026-08-01" in _tooltip_texts(fake_ui)


def test_every_remaining_tooltip_is_width_capped_and_wrapping():
    fake_ui = _render([ExpertMetric("Analysts", 16, "16", detail="Period: 2026-08-01")])
    styles = [c.args[0] for c in fake_ui.tooltip.return_value.style.call_args_list if c.args]
    assert DETAIL_TOOLTIP_STYLE in styles, styles


# --------------------------------------------------------------------------
# "All analysts data" -- a table, not prose
# --------------------------------------------------------------------------

def test_a_detail_table_is_rendered_as_a_table_not_a_sentence():
    rows = [("Strong Buy", "0"), ("Buy", "7"), ("Hold", "6"),
            ("Sell", "3"), ("Strong Sell", "0")]
    fake_ui = _render([ExpertMetric("Analyst ratings", 16, "16 analysts",
                                    detail_table=rows)])
    assert fake_ui.table.call_args_list, "no ui.table was created for detail_table"
    kwargs = fake_ui.table.call_args_list[0].kwargs
    rendered = {(r["k"], r["v"]) for r in kwargs["rows"]}
    assert rendered == set(rows)


def test_a_metric_with_neither_detail_nor_table_draws_no_panel_and_no_tooltip():
    fake_ui = _render([ExpertMetric("Macro regime", 0.78, "+0.78", "buy")])
    titles = [c.args[0] for c in fake_ui.expansion.call_args_list if c.args]
    assert not any("Macro regime" in t for t in titles)
    assert _tooltip_texts(fake_ui) == []


# --------------------------------------------------------------------------
# The header confidence
# --------------------------------------------------------------------------

def _render_export(export):
    tab = symbol360.Symbol360Tab.__new__(symbol360.Symbol360Tab)
    fake_ui = MagicMock(name="ui")
    with patch.object(symbol360, "ui", fake_ui):
        tab._render_export_card("FactorRanker", export)
    return fake_ui


def test_a_declared_unavailable_confidence_is_not_printed_as_zero_percent():
    export = ExpertDataExport(
        expert_name="FactorRanker", symbol="AAPL", overall_signal="buy",
        confidence=None,
        confidence_unavailable_reason="FactorRanker ranks a universe against itself.",
        metrics=[], settings_used={}, relevant_settings=())
    texts = _all_text(_render_export(export))
    assert not any("Confidence: 0.0%" in t for t in texts), texts
    assert any("Confidence: n/a" in t for t in texts), texts
    assert any("ranks a universe" in t for t in texts), texts


def test_a_declared_unavailable_signal_draws_no_badge_but_says_why():
    export = ExpertDataExport(
        expert_name="FactorRanker", symbol="AAPL", overall_signal=None,
        signal_unavailable_reason="FactorRanker is basket-level.",
        metrics=[], settings_used={}, relevant_settings=())
    fake_ui = _render_export(export)
    badges = [c.args[0] for c in fake_ui.badge.call_args_list if c.args]
    assert "BUY" not in badges
    assert any("basket-level" in t for t in _all_text(fake_ui))


def test_a_measured_confidence_is_still_printed():
    export = ExpertDataExport(expert_name="FMPRating", symbol="AAPL",
                              confidence=43.8, metrics=[], settings_used={},
                              relevant_settings=())
    assert any("Confidence: 43.8%" in t for t in _all_text(_render_export(export)))


def test_a_measured_zero_confidence_is_still_printed_as_zero():
    """Inverse error: 0.0% from an expert that really measured it is data."""
    export = ExpertDataExport(expert_name="FMPRating", symbol="AAPL",
                              confidence=0.0, metrics=[], settings_used={},
                              relevant_settings=())
    assert any("Confidence: 0.0%" in t for t in _all_text(_render_export(export)))


def test_the_card_still_renders_its_error_and_skip_states():
    """Guard the early-return paths the detail work runs alongside."""
    tab = symbol360.Symbol360Tab.__new__(symbol360.Symbol360Tab)
    fake_ui = MagicMock(name="ui")
    with patch.object(symbol360, "ui", fake_ui):
        tab._render_export_card("X", ExpertDataExport(expert_name="X", symbol="AAPL",
                                                      error="boom"))
    assert any("boom" in t for t in _all_text(fake_ui))
