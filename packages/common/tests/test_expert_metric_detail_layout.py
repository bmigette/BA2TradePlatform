"""How an ExpertMetric's ``detail`` must be PRESENTED (pure layer).

The defect this pins down: SYMBOL360 renders every ``detail`` as a hover
tooltip (``ui.icon(...).tooltip(text)``). A tooltip is HTML, so the newlines in
a multi-step derivation collapse to spaces and the whole thing comes out as ONE
line wider than the viewport, clipped at both ends and impossible to scroll,
select or copy. FMPRating's ``details`` (analyst counts + price targets + a
4-step confidence derivation) is ~1.5 kB of structured text rendered exactly
that way.

The decision of WHERE a detail goes (short one-liner tooltip vs expandable
panel) is pure, so it lives here rather than in the renderer -- the renderer
only draws what ``plan_metric_detail`` decides. ``nicegui.testing`` is used
nowhere in this repo and is not needed to test the rule that matters.
"""
import pytest

from ba2_common.core.interfaces.ExpertDataExportInterface import (
    DETAIL_INLINE_MAX_CHARS,
    DETAIL_TOOLTIP_STYLE,
    ExpertMetric,
    plan_metric_detail,
)


def test_no_detail_at_all_renders_nothing():
    layout = plan_metric_detail(ExpertMetric("Macro regime", 0.78, "+0.78"))
    assert layout.mode == "none"
    assert layout.lines == []
    assert layout.table == []


def test_a_short_one_line_detail_stays_a_tooltip():
    m = ExpertMetric("Analysts", 16, "16", detail="Period: 2026-08-01")
    layout = plan_metric_detail(m)
    assert layout.mode == "tooltip"
    assert layout.text == "Period: 2026-08-01"


def test_a_multi_line_detail_is_never_a_tooltip():
    """THE defect: newlines collapse in a tooltip, so anything with structure
    must go to a panel no matter how short it is."""
    m = ExpertMetric("Recommendation", "BUY", "BUY (100%)",
                     detail="Step 1 - Weighted Scores\nStep 2 - Base Confidence")
    layout = plan_metric_detail(m)
    assert layout.mode == "panel"
    assert layout.lines == ["Step 1 - Weighted Scores", "Step 2 - Base Confidence"]


def test_a_long_single_line_detail_is_a_panel_not_an_unwrapped_tooltip():
    """No newlines, but far too wide for a hover bubble."""
    long_text = "x" * (DETAIL_INLINE_MAX_CHARS + 1)
    layout = plan_metric_detail(ExpertMetric("L", 1, "1", detail=long_text))
    assert layout.mode == "panel"


def test_the_boundary_length_is_still_a_tooltip():
    """Exactly at the limit is short enough -- guards against an off-by-one that
    would push every short detail into a panel."""
    text = "y" * DETAIL_INLINE_MAX_CHARS
    assert plan_metric_detail(ExpertMetric("L", 1, "1", detail=text)).mode == "tooltip"


def test_a_detail_table_always_forces_a_panel_even_with_no_text():
    m = ExpertMetric("Analyst ratings", None, "16 analysts",
                     detail_table=[("Strong Buy", "0"), ("Buy", "7")])
    layout = plan_metric_detail(m)
    assert layout.mode == "panel"
    assert layout.table == [("Strong Buy", "0"), ("Buy", "7")]


def test_windows_line_endings_and_trailing_blank_lines_are_normalized():
    m = ExpertMetric("L", 1, "1", detail="a\r\nb\r\n\n   \n")
    layout = plan_metric_detail(m)
    assert layout.lines == ["a", "b"]
    assert layout.text == "a\nb"


def test_a_whitespace_only_detail_is_treated_as_absent():
    assert plan_metric_detail(ExpertMetric("L", 1, "1", detail="   \n  ")).mode == "none"


def test_panel_title_names_the_metric_so_several_panels_stay_distinguishable():
    m = ExpertMetric("Fundamental section", -0.30, "-0.30", detail="a\nb")
    assert plan_metric_detail(m).title == "Fundamental section — details"


def test_the_tooltip_style_wraps_and_is_width_capped():
    """Whatever remains a tooltip must never be able to overflow the viewport
    again: the shared style has to bound its width AND allow wrapping."""
    assert "max-width" in DETAIL_TOOLTIP_STYLE
    assert "pre-line" in DETAIL_TOOLTIP_STYLE   # honour the (single) newline-free text
    assert "white-space" in DETAIL_TOOLTIP_STYLE


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_blank_detail_with_a_table_still_yields_the_table(bad):
    m = ExpertMetric("L", 1, "1", detail=bad, detail_table=[("a", "b")])
    layout = plan_metric_detail(m)
    assert layout.mode == "panel"
    assert layout.lines == []
    assert layout.table == [("a", "b")]
