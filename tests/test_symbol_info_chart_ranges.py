"""The symbol-info chart's zoom window and range buttons.

Asked for from live use on 2026-09-05: make the comparison chart zoomable, taller, and
give it YTD / 1Y / 3Y / 5Y / 10Y range buttons.
"""
from datetime import date

import pytest

from ba2_trade_platform.ui.components import symbol_info_panel as panel

# Sparse on purpose: real categories are trading days, so the boundary a range asks for
# is usually a weekend or a holiday and is NOT itself a category.
CATS = ['2016-01-04', '2021-09-07', '2022-02-17', '2023-06-30',
        '2024-05-02', '2025-03-03', '2026-09-04']
TODAY = date(2026, 9, 4)


class TestRangeStart:
    def test_all_starts_at_the_first_category(self):
        assert panel.range_start_iso(CATS, 'all', TODAY) == CATS[0]

    def test_ytd_starts_at_the_first_category_of_this_year(self):
        assert panel.range_start_iso(CATS, 'ytd', TODAY) == '2026-09-04'

    def test_three_years_lands_on_a_real_category(self):
        # 2023-09-04 is in no category; the first one AT OR AFTER it is.
        assert panel.range_start_iso(CATS, '3y', TODAY) == '2024-05-02'

    def test_ten_years_excludes_a_category_older_than_the_window(self):
        # 10Y back from 2026-09-04 is 2016-09-04, and 2016-01-04 is BEFORE that — so
        # the window opens at the next category, not at the start of the history.
        assert panel.range_start_iso(CATS, '10y', TODAY) == '2021-09-07'

    def test_a_category_inside_the_window_is_kept(self):
        cats = ['2016-01-04', '2016-11-01', '2026-09-04']
        assert panel.range_start_iso(cats, '10y', TODAY) == '2016-11-01'

    def test_a_window_longer_than_the_history_shows_all_of_it(self):
        # "10Y" on three years of data is three years, not an empty chart.
        short = ['2024-01-02', '2025-01-02', '2026-01-02']
        assert panel.range_start_iso(short, '10y', TODAY) == short[0]

    def test_the_returned_value_is_always_a_real_category(self):
        # ECharts matches dataZoom.startValue against the category values, so a
        # computed date that is not one of them silently zooms nowhere.
        for _, key in panel.CHART_RANGES:
            assert panel.range_start_iso(CATS, key, TODAY) in CATS

    def test_no_categories_gives_no_window(self):
        assert panel.range_start_iso([], '1y', TODAY) is None

    def test_an_unknown_key_falls_back_to_the_whole_history(self):
        assert panel.range_start_iso(CATS, 'nonsense', TODAY) == CATS[0]

    def test_a_leap_day_today_does_not_crash(self):
        leap = ['2019-03-01', '2020-02-29', '2024-02-29']
        assert panel.range_start_iso(leap, '1y', date(2024, 2, 29)) == '2024-02-29'


class TestZoomOptions:
    def test_both_a_pinch_zoom_and_a_slider_are_present(self):
        zooms = panel._data_zoom(CATS)
        assert {z['type'] for z in zooms} == {'inside', 'slider'}

    def test_they_open_on_the_same_window(self):
        # A slider showing a window the plot is not in reads as a broken control.
        inside, slider = panel._data_zoom(CATS)
        assert inside['startValue'] == slider['startValue']
        assert inside['endValue'] == slider['endValue']

    def test_the_default_window_is_the_whole_history(self):
        # The chart has always opened on everything; a range control that cropped it
        # on open would change what is being looked at without being asked.
        assert panel.DEFAULT_CHART_RANGE == 'all'
        inside, _ = panel._data_zoom(CATS)
        assert (inside['startValue'], inside['endValue']) == (CATS[0], CATS[-1])

    def test_it_survives_an_empty_series(self):
        inside, _ = panel._data_zoom([])
        assert inside['startValue'] is None and inside['endValue'] is None


class TestChartOptionsCarryTheZoom:
    def _info(self, symbol='AAA'):
        from ba2_providers.symbol_info import get_symbols_info  # noqa: F401 — import parity
        return None  # built via the panel's own fixtures below

    def test_the_comparison_chart_declares_a_zoom(self, monkeypatch):
        opts = panel.build_comparison_chart_options([])
        assert 'dataZoom' in opts
        assert {z['type'] for z in opts['dataZoom']} == {'inside', 'slider'}

    def test_the_grid_leaves_room_for_the_slider(self):
        # The slider is drawn UNDER the plot; at the old bottom=50 it landed on the
        # x-axis labels.
        opts = panel.build_comparison_chart_options([])
        assert opts['grid']['bottom'] >= 70


class TestNothingOverlaps:
    """Three things compete for the bottom of the chart and two for the top-right.

    Reported from live use 2026-09-05: the legend was drawn on top of the zoom
    slider, and the two right-hand axis names collided into one unreadable smear.
    """

    def _stack(self, opts):
        slider = next(z for z in opts['dataZoom'] if z['type'] == 'slider')
        return slider['bottom'] + slider['height'], opts['legend']['bottom']

    @pytest.mark.parametrize('build', [
        lambda: panel.build_comparison_chart_options([]),
    ])
    def test_the_legend_clears_the_zoom_slider(self, build):
        slider_top, legend_bottom = self._stack(build())
        assert legend_bottom > slider_top

    def test_the_plot_clears_the_legend(self):
        opts = panel.build_comparison_chart_options([])
        # grid.bottom is measured from the same edge as legend.bottom.
        assert opts['grid']['bottom'] > opts['legend']['bottom']

    def test_the_legend_is_anchored_rather_than_left_to_the_default(self):
        # Unpositioned, ECharts put it exactly where the slider already was.
        assert 'bottom' in panel.build_comparison_chart_options([])['legend']

    def test_the_two_right_hand_axis_names_are_staggered(self):
        # Same side, only ``offset`` apart horizontally, and both names are long --
        # so they are separated VERTICALLY instead.
        assert panel.NAME_GAP_STACKED > panel.NAME_GAP_DEFAULT

    def test_a_stacked_axis_asks_for_the_larger_gap(self):
        axis = panel._axis('x', position='right', offset=70,
                           name_gap=panel.NAME_GAP_STACKED)
        assert axis['nameGap'] == panel.NAME_GAP_STACKED

    def test_an_ordinary_axis_keeps_the_default_gap(self):
        assert panel._axis('x', position='left')['nameGap'] == panel.NAME_GAP_DEFAULT

    def test_the_chart_is_taller_than_it_was(self):
        assert panel.CHART_HEIGHT_PX >= 500


class TestInertRangesAreVisiblyDead:
    """Reported 2026-09-05: "clicking 10Y or Max doesn't seem to do anything".

    It was doing exactly what it should. The symbol carried ~5 weeks of prices, so
    every range resolved to the whole history — the view already on screen. Correct
    arithmetic, invisible UI. A range that cannot crop anything is now disabled and
    says why.
    """

    FIVE_WEEKS = ['2026-07-29', '2026-08-19', '2026-09-04']
    FIVE_YEARS = ['2021-09-07', '2024-05-02', '2026-09-04']

    def test_every_range_is_dead_on_five_weeks_of_history(self):
        for _, key in panel.CHART_RANGES:
            if key == 'all':
                continue
            assert not panel.range_is_usable(self.FIVE_WEEKS, key, TODAY), key

    def test_max_stays_live_because_it_is_the_way_back(self):
        # Even when it is where you already are: it resets a manual pinch-zoom.
        assert panel.range_is_usable(self.FIVE_WEEKS, 'all', TODAY)
        assert panel.range_is_usable(self.FIVE_YEARS, 'all', TODAY)

    def test_the_ranges_that_can_crop_are_live(self):
        for key in ('ytd', '1y', '3y'):
            assert panel.range_is_usable(self.FIVE_YEARS, key, TODAY), key

    def test_ranges_longer_than_the_history_are_dead(self):
        # 5Y and 10Y over five years of data both show all of it.
        for key in ('5y', '10y'):
            assert not panel.range_is_usable(self.FIVE_YEARS, key, TODAY), key

    def test_no_categories_means_nothing_is_usable(self):
        assert not panel.range_is_usable([], 'all')
        assert not panel.range_is_usable([], '1y')

    def test_the_reason_names_the_actual_span(self):
        text = panel.RANGE_UNAVAILABLE_FMT.format(
            span=panel._describe_span(self.FIVE_WEEKS[0], self.FIVE_WEEKS[-1]),
            first=self.FIVE_WEEKS[0], last=self.FIVE_WEEKS[-1])
        assert 'weeks' in text
        assert '2026-07-29' in text and '2026-09-04' in text

    def test_the_button_says_Max_not_All(self):
        assert ('Max', 'all') in panel.CHART_RANGES


class TestSpanWording:
    def test_it_scales_with_the_length(self):
        assert 'days' in panel._describe_span('2026-09-01', '2026-09-04')
        assert 'weeks' in panel._describe_span('2026-07-29', '2026-09-04')
        assert 'months' in panel._describe_span('2026-01-04', '2026-09-04')
        assert panel._describe_span('2025-06-04', '2026-09-04') == 'about a year'
        assert panel._describe_span('2021-09-07', '2026-09-04') == '4 years'

    def test_a_single_day_never_reads_as_zero_or_as_bad_grammar(self):
        assert panel._describe_span('2026-09-04', '2026-09-04') == '1 day'

    def test_junk_dates_do_not_raise(self):
        assert panel._describe_span('not-a-date', '2026-09-04') == 'the available history'


class TestPlottedPrecision:
    """The tooltip prints the data array verbatim, so the rounding lives there."""

    def test_a_percentage_is_rounded_for_the_tooltip(self):
        # Reported: "126.23655913978493" in the tooltip.
        out = panel._aligned({'d': 126.23655913978493}, ['d'], digits=2)
        assert out == [126.24]

    def test_a_missing_observation_stays_a_gap_not_a_zero(self):
        assert panel._aligned({'a': 1.0}, ['a', 'b'], digits=2) == [1.0, None]

    def test_unrounded_by_default(self):
        # Dividends live in the sub-cent; 2dp would round a real payment to zero.
        assert panel._aligned({'d': 0.0008}, ['d']) == [0.0008]
