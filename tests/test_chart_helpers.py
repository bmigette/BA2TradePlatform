"""``ui/utils/chart_helpers``: the $/% conversion, the legend that must not overlap.

The Growth-by-Label chart drew thirty series names in a legend with no height limit, so
it wrapped onto four rows and grew DOWN over the plot -- covering the top gridline and
the "$1,800" label beside it. And the two growth charts had no $/% switch at all, unlike
the monthly ones.
"""
import pytest

from ba2_trade_platform.ui.utils.chart_helpers import (
    AXIS_FORMAT_MONEY,
    label_series_colors,
    AXIS_FORMAT_PCT,
    LEGEND_SCROLL_THRESHOLD,
    axis_format,
    grid_options,
    legend_options,
    pct_of_invested,
)


# ---------------------------------------------------------------------------
# % of invested
# ---------------------------------------------------------------------------

def test_a_value_is_expressed_against_the_capital_put_in():
    assert pct_of_invested([115.0, 90.0], [100.0, 100.0]) == [115.0, 90.0]
    assert pct_of_invested([150.0], [50.0]) == [300.0]


def test_the_invested_series_against_itself_is_a_flat_hundred():
    """Which is the point: it becomes the reference line the others are read against."""
    invested = [100.0, 250.0, 400.0]

    assert pct_of_invested(invested, invested) == [100.0, 100.0, 100.0]


def test_no_invested_capital_is_a_GAP_and_never_zero_percent():
    """Before the first purchase there is no denominator. A 0% would draw a flat line
    along the axis claiming the position existed and returned nothing; ECharts renders
    None as a break, which is the truth."""
    assert pct_of_invested([0.0, 120.0], [0.0, 100.0]) == [None, 120.0]
    assert pct_of_invested([120.0], [None]) == [None]


def test_an_unmeasurable_value_stays_unmeasurable():
    assert pct_of_invested([None], [100.0]) == [None]


def test_a_shorter_denominator_pads_with_gaps_rather_than_raising():
    """These are parallel per-date accumulations; a mismatch means one series started
    later, which is a gap and not an error."""
    assert pct_of_invested([100.0, 200.0, 300.0], [100.0]) == [100.0, None, None]


def test_the_axis_format_follows_the_mode():
    assert axis_format(False) == AXIS_FORMAT_MONEY
    assert axis_format(True) == AXIS_FORMAT_PCT


# ---------------------------------------------------------------------------
# the legend that reported the bug
# ---------------------------------------------------------------------------

def test_a_short_legend_stays_plain():
    """A control that does nothing is worse than no control: three series do not need
    pagination arrows."""
    options = legend_options(['a', 'b', 'c'])

    assert 'type' not in options
    assert options['data'] == ['a', 'b', 'c']


def test_a_long_legend_paginates_instead_of_wrapping_over_the_plot():
    names = [f'LABEL_{i}' for i in range(30)]

    options = legend_options(names)

    assert options['type'] == 'scroll'


def test_the_paginator_is_legible_on_a_dark_background():
    """ECharts' default page arrows are near-black and vanish on this theme."""
    options = legend_options([f'L{i}' for i in range(30)])

    assert options['pageIconColor'] == '#a0aec0'
    assert options['pageTextStyle']['color'] == '#a0aec0'


def test_the_plot_starts_below_a_wrapping_legend():
    """The grid reads the SAME list as the legend, so the two cannot drift: a legend
    that needs three rows must not be given a plot that starts under two."""
    few = grid_options(['a', 'b', 'c'])
    many = grid_options([f'L{i}' for i in range(LEGEND_SCROLL_THRESHOLD)])

    assert many['top'] > few['top'], "more rows must push the plot further down"


def test_a_paginated_legend_needs_only_one_rows_worth_of_space():
    """Whatever the entry count -- that is what pagination buys."""
    thirty = grid_options([f'L{i}' for i in range(30)])
    three_hundred = grid_options([f'L{i}' for i in range(300)])

    assert thirty['top'] == three_hundred['top']


# ---------------------------------------------------------------------------
# A NEGATIVE denominator — the margin-call spike
# ---------------------------------------------------------------------------

def test_a_negative_invested_base_is_a_gap_not_a_huge_negative_percent():
    """Reported: one day dropped to -1,200% and flattened every other series onto the
    axis. A cost accumulator that reduced on a sale by the PROCEEDS rather than by the
    cost of the shares sold went below zero on a profitable forced liquidation, and
    ``value / -8 * 100`` is a perfectly finite -1,200%.

    A percentage OF a negative capital base is not a large number; it is not a quantity.
    """
    assert pct_of_invested([100.0], [-8.0]) == [None]


def test_the_series_around_the_gap_is_untouched():
    """Only the unmeasurable point breaks; the chart keeps its scale."""
    values = [110.0, 120.0, 100.0, 130.0]
    invested = [100.0, 100.0, -8.0, 100.0]

    assert pct_of_invested(values, invested) == [110.0, 120.0, None, 130.0]


# ---------------------------------------------------------------------------
# Label colours shared with Portfolio Allocation
# ---------------------------------------------------------------------------

PALETTE = ['#111111', '#222222', '#333333']


def test_a_label_coloured_on_the_allocation_page_keeps_that_colour():
    colors = label_series_colors(['A', 'B'], PALETTE, {'A': '#FF9800'})

    assert colors[0] == '#FF9800'


def test_an_uncoloured_label_falls_back_to_the_palette_by_position():
    colors = label_series_colors(['A', 'B', 'C'], PALETTE, {})

    assert colors == PALETTE


def test_a_stored_colour_does_not_shift_the_palette_for_its_neighbours():
    """Position-based fallback stays keyed on the label's own index, so colouring one
    label cannot recolour the ones after it."""
    colors = label_series_colors(['A', 'B', 'C'], PALETTE, {'B': '#FF9800'})

    assert colors == ['#111111', '#FF9800', '#333333']


def test_an_unparseable_stored_colour_uses_the_palette_rather_than_grey():
    """``resolve_label_icon_color`` answers the neutral grey for anything it cannot
    parse -- right for an icon, wrong for a chart, where three unparseable labels would
    become three identical grey lines."""
    colors = label_series_colors(['A', 'B'], PALETTE, {'A': 'not-a-colour',
                                                       'B': 'also-bad'})

    assert colors == ['#111111', '#222222']


def test_no_stored_colours_at_all_is_the_plain_palette():
    assert label_series_colors(['A', 'B'], PALETTE, None) == ['#111111', '#222222']
