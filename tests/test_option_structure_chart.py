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


class TestPayoffOverlay:
    def test_the_curve_is_on_the_second_x_axis_with_pnl_on_x(self):
        curve = build_payoff_chart([_leg()])
        figure = build_option_structure_figure(bars=BARS, payoff=curve)

        overlay = _traces(figure)
        assert overlay, 'the payoff must be drawn on xaxis2'
        named = [t for t in overlay if t.name == 'Expiration payoff']
        assert len(named) == 1
        # Rotated: x is P&L, y is the underlying price the candles use.
        assert max(named[0].y) <= max(bar['high'] for bar in BARS) + 1e-6
        assert any(value < 0 for value in named[0].x) and any(value > 0 for value in named[0].x)

    def test_each_sign_run_is_filled_to_the_zero_line(self):
        curve = build_payoff_chart([_leg()])
        figure = build_option_structure_figure(bars=BARS, payoff=curve)
        fills = [t for t in _traces(figure) if t.fill == 'tozerox']
        assert len(fills) == 2                      # one loss run, one profit run
        assert {t.line.color for t in fills} == {'#16a34a', '#dc2626'}

    def test_a_straddle_fills_three_runs(self):
        curve = build_payoff_chart([_leg(),
                              _leg(kind='put', premium=4)])
        figure = build_option_structure_figure(bars=BARS, payoff=curve)
        assert len([t for t in _traces(figure) if t.fill == 'tozerox']) == 3

    def test_the_zero_line_is_vertical_on_the_pnl_axis(self):
        figure = build_option_structure_figure(bars=BARS, payoff=build_payoff_chart([_leg()]))
        vertical = [s for s in figure.layout.shapes if getattr(s, 'xref', None) == 'x2']
        assert any(s.x0 == 0 and s.x1 == 0 for s in vertical)

    def test_the_pnl_axis_sits_on_top_and_is_symmetric(self):
        figure = build_option_structure_figure(bars=BARS, payoff=build_payoff_chart([_leg()]))
        assert figure.layout.xaxis2.side == 'top'
        low, high = figure.layout.xaxis2.range
        assert low == pytest.approx(-high)

    def test_overlay_off_keeps_the_bands_but_draws_no_curve(self):
        curve = build_payoff_chart([_leg()])
        figure = build_option_structure_figure(bars=BARS, payoff=curve, show_overlay=False)
        assert not _traces(figure)
        assert not [s for s in figure.layout.shapes if getattr(s, 'xref', None) == 'x2']
        bands = [s for s in figure.layout.shapes if getattr(s, 'fillcolor', None) in ('#16a34a', '#dc2626')]
        assert bands, 'the sign-only bands are what remains'

    def test_an_unavailable_payoff_draws_no_curve_and_no_bands(self):
        figure = build_option_structure_figure(
            bars=BARS, payoff=PayoffUnavailable('leg 2: contract multiplier not recorded'))
        assert not _traces(figure)
        assert not [s for s in figure.layout.shapes if getattr(s, 'fillcolor', None) in ('#16a34a', '#dc2626')]

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
