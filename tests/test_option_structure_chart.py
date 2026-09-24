"""The live option popup's chart figure (spec 2026-09-20, steps 9 and 10).

The figure is built by a pure function precisely so it can be asserted trace by trace. These
tests are the substitute for looking at it: they pin the rotated payoff to the SECOND x-axis
(decision 1a), the two marker sets (2c), and the rule that green/red are P&L while strikes
and moneyness are neutral.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.option_payoff import PayoffLeg
from ba2_common.core.option_payoff_chart import (
    ChartLeg, PayoffUnavailable, build_payoff_chart,
)
from ba2_common.core.types import OrderDirection
from ba2_trade_platform.ui.components.option_structure_chart import (
    build_option_structure_figure, chart_inputs_from, normalise_bars,
)

BARS = [
    {'date': '2026-09-08', 'open': 100.0, 'high': 103.0, 'low': 99.0, 'close': 102.0},
    {'date': '2026-09-09', 'open': 102.0, 'high': 106.0, 'low': 101.0, 'close': 105.0},
    {'date': '2026-09-10', 'open': 105.0, 'high': 109.0, 'low': 104.0, 'close': 108.0},
]


def _leg(kind='call', side=OrderDirection.BUY, premium=5.0, strike=100.0,
         ratio=1, multiplier=100.0, **chart):
    return ChartLeg(
        payoff=PayoffLeg(kind=kind, side=side, premium=premium, strike=strike,
                         ratio=ratio, multiplier=multiplier),
        expiry=chart.pop('expiry', '2026-09-18'),
        underlying=chart.pop('underlying', 'ACN'),
        **chart,
    )


def _order(**over):
    base = dict(id=1, contract_symbol='ACN260918C00100000', strike=100.0, quantity=1.0,
                multiplier=100, option_type=SimpleNamespace(value='call'),
                side=SimpleNamespace(value='BUY'), open_price=5.0, status=SimpleNamespace(value='FILLED'),
                created_at=datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc))
    base.update(over)
    return SimpleNamespace(**base)


def _txn(**over):
    base = dict(id=7, symbol='ACN', open_date=datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc),
                close_date=None)
    base.update(over)
    return SimpleNamespace(**base)


def _traces(figure, axis='x2'):
    return [trace for trace in figure.data if getattr(trace, 'xaxis', None) == axis]


class TestBars:
    def test_a_bar_with_a_missing_price_is_dropped_not_zero_filled(self):
        bars = normalise_bars([
            {'date': '2026-09-08', 'open': 100, 'high': 103, 'low': 99, 'close': 102},
            {'date': '2026-09-09', 'open': None, 'high': 106, 'low': 101, 'close': 105},
        ])
        assert [bar['date'] for bar in bars] == ['2026-09-08']

    def test_bars_are_sorted_and_accept_a_dataframe_shape(self):
        bars = normalise_bars([
            {'Date': '2026-09-10', 'Open': 105, 'High': 109, 'Low': 104, 'Close': 108},
            {'Date': '2026-09-08', 'Open': 100, 'High': 103, 'Low': 99, 'Close': 102},
        ])
        assert [bar['date'] for bar in bars] == ['2026-09-08', '2026-09-10']

    def test_the_figure_draws_one_candlestick_series(self):
        figure = build_option_structure_figure(bars=BARS)
        candles = [trace for trace in figure.data if trace.type == 'candlestick']
        assert len(candles) == 1
        assert list(candles[0].x) == [bar['date'] for bar in BARS]
        assert list(candles[0].close) == [bar['close'] for bar in BARS]

    def test_no_bars_still_builds_a_figure(self):
        figure = build_option_structure_figure(bars=[], payoff=build_payoff_chart([_leg()]))
        assert not [trace for trace in figure.data if trace.type == 'candlestick']


class TestStrikeLines:
    def test_one_dashed_line_per_distinct_strike(self):
        figure = build_option_structure_figure(
            bars=BARS,
            strike_lines=[{'price': 95.0, 'label': 'Long 1 Call · K $95'},
                          {'price': 105.0, 'label': 'Short 1 Call · K $105'}],
        )
        horizontal = [s for s in figure.layout.shapes if s.y0 is not None and s.y0 == s.y1]
        assert sorted(s.y0 for s in horizontal) == [95.0, 105.0]
        # Plotly puts an hline's label in the figure's ANNOTATIONS, not on the shape.
        labels = [annotation.text or '' for annotation in figure.layout.annotations]
        assert any('K $95' in text for text in labels)
        assert any('K $105' in text for text in labels)


class TestPayoffOnTheTimeChart:
    """2026-09-24: the rotated payoff curve is gone (it put dollars on the time axis). What a
    time chart keeps is the breakeven line and the profit/loss zones either side of it."""

    def test_no_payoff_curve_and_no_second_x_axis(self):
        figure = build_option_structure_figure(bars=BARS, payoff=build_payoff_chart([_leg()]))
        assert not _traces(figure)
        assert not [t for t in figure.data if t.name == 'Expiration payoff']
        assert not [s for s in figure.layout.shapes if getattr(s, 'xref', None) == 'x2']

    def test_the_breakeven_is_a_labelled_horizontal_line(self):
        figure = build_option_structure_figure(bars=BARS, payoff=build_payoff_chart([_leg()]))
        lines = [s for s in figure.layout.shapes if getattr(s.line, 'color', None) == '#eab308']
        assert [round(s.y0, 2) for s in lines] == [105.0]           # strike 100 + premium 5
        labels = [a.text for a in figure.layout.annotations]
        assert 'Breakeven at expiry $105.00' in labels

    def test_a_straddle_draws_both_breakevens(self):
        curve = build_payoff_chart([_leg(), _leg(kind='put', premium=4)])
        figure = build_option_structure_figure(bars=BARS, payoff=curve)
        lines = [s for s in figure.layout.shapes if getattr(s.line, 'color', None) == '#eab308']
        assert len(lines) == 2

    def test_zones_are_drawn_and_follow_the_toggle(self):
        curve = build_payoff_chart([_leg()])
        on = build_option_structure_figure(bars=BARS, payoff=curve)
        off = build_option_structure_figure(bars=BARS, payoff=curve, show_overlay=False)
        colour = lambda fig: [s for s in fig.layout.shapes
                              if getattr(s, 'fillcolor', None) in ('#16a34a', '#dc2626')]
        assert colour(on) and not colour(off)

    def test_an_unavailable_payoff_draws_no_breakeven_and_no_zones(self):
        figure = build_option_structure_figure(
            bars=BARS, payoff=PayoffUnavailable('leg 2: contract multiplier not recorded'))
        assert not [s for s in figure.layout.shapes if getattr(s, 'fillcolor', None) in ('#16a34a', '#dc2626')]
        assert not [s for s in figure.layout.shapes if getattr(s.line, 'color', None) == '#eab308']

    def test_weekends_are_skipped_on_the_time_axis(self):
        figure = build_option_structure_figure(bars=BARS, payoff=build_payoff_chart([_leg()]))
        assert list(figure.layout.xaxis.rangebreaks[0].bounds) == ['sat', 'mon']

    def test_the_price_axis_never_goes_negative(self):
        curve = build_payoff_chart([_leg(kind='put')])
        figure = build_option_structure_figure(bars=BARS, payoff=curve)
        assert figure.layout.yaxis.range[0] >= 0


class TestMarkers:
    def test_both_marker_sets_are_drawn(self):
        markers = [
            {'date': '2026-09-08', 'kind': 'structure', 'text': 'Structure entry', 'price': 102.0},
            {'date': '2026-09-09', 'kind': 'leg', 'text': 'Long Call $100 order placed', 'price': 105.0},
        ]
        figure = build_option_structure_figure(bars=BARS, markers=markers)
        names = {trace.name for trace in figure.data}
        assert names == {'Underlying', 'Structure', 'Leg orders'}

    def test_either_set_can_be_switched_off(self):
        markers = [
            {'date': '2026-09-08', 'kind': 'structure', 'text': 'entry', 'price': 102.0},
            {'date': '2026-09-09', 'kind': 'leg', 'text': 'leg', 'price': 105.0},
        ]
        only_structure = build_option_structure_figure(
            bars=BARS, markers=markers, show_leg_markers=False)
        assert 'Leg orders' not in {t.name for t in only_structure.data}

        only_legs = build_option_structure_figure(
            bars=BARS, markers=markers, show_structure_markers=False)
        assert 'Structure' not in {t.name for t in only_legs.data}

    def test_a_marker_without_a_date_is_skipped(self):
        figure = build_option_structure_figure(
            bars=BARS, markers=[{'date': None, 'kind': 'leg', 'text': 'x', 'price': 1.0}])
        assert 'Leg orders' not in {t.name for t in figure.data}


class TestChartInputs:
    def test_strikes_and_markers_come_from_the_transaction_and_its_orders(self):
        bars, strikes, markers = chart_inputs_from(
            _txn(close_date=datetime(2026, 9, 10, 19, 45, tzinfo=timezone.utc)),
            [_order(), _order(id=2, strike=105.0, side=SimpleNamespace(value='SELL'),
                              option_type=SimpleNamespace(value='call'), open_price=2.0)],
            BARS,
        )
        assert [line['price'] for line in strikes] == [100.0, 105.0]
        assert 'Long 1 Call · K $100.00' in strikes[0]['label']
        assert 'Short 1 Call · K $105.00' in strikes[1]['label']

        kinds = [marker['kind'] for marker in markers]
        assert kinds.count('structure') == 2
        assert kinds.count('leg') == 2
        assert any('order placed' in marker['text'] for marker in markers)

    def test_a_leg_without_a_strike_is_not_given_one(self):
        _bars, strikes, markers = chart_inputs_from(_txn(), [_order(strike=None)], BARS)
        assert strikes == []
        assert [m for m in markers if m['kind'] == 'leg']

    def test_markers_are_labelled_as_order_placement_not_a_fill_price(self):
        # A live order carries no per-leg fill timestamp, so the marker must not imply one.
        _bars, _strikes, markers = chart_inputs_from(_txn(), [_order()], BARS)
        leg_marker = [m for m in markers if m['kind'] == 'leg'][0]
        assert leg_marker['date'] == '2026-09-08'
        assert 'order placed' in leg_marker['text']


def test_same_bar_markers_are_spaced_by_the_chart_range_not_the_candle():
    """On a narrow candle the old step (16% of the bar) was invisible: 'Entry' printed over
    the leg label. Two markers on one bar now sit at least 6% of the chart's range apart."""
    bars = BARS + [{'date': '2026-09-11', 'open': 108.0, 'high': 108.2, 'low': 107.9,
                    'close': 108.1}]
    txn = _txn(open_date=datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc))
    order = _order(created_at=datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc))
    _, _, markers = chart_inputs_from(txn, [order], bars)
    same_day = sorted(m['price'] for m in markers if m['date'] == '2026-09-11')
    assert len(same_day) == 2
    chart_range = max(b['high'] for b in bars) - min(b['low'] for b in bars)
    assert same_day[1] - same_day[0] >= 0.06 * chart_range - 1e-9


def test_the_chart_blends_into_the_popup_and_has_no_zoom_toolbar():
    """2026-09-24: plotly_dark's near-black panel inside the card, and a zoom/pan toolbar
    nobody needs on a popup."""
    from ba2_trade_platform.ui.components.option_structure_chart import PLOTLY_CONFIG

    figure = build_option_structure_figure(bars=BARS, payoff=build_payoff_chart([_leg()]))
    assert figure.layout.paper_bgcolor == 'rgba(0,0,0,0)'
    assert figure.layout.plot_bgcolor == 'rgba(0,0,0,0)'
    assert figure.layout.xaxis.fixedrange and figure.layout.yaxis.fixedrange
    assert PLOTLY_CONFIG['displayModeBar'] is False
