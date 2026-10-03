"""Account Growth chart layout decisions: month axis, date ticks, stacked bars, null-before-start."""
from datetime import date
from pathlib import Path

import pytest

from ba2_trade_platform.ui.utils.chart_axes import (
    MAX_DETAIL_LABELS, TOTAL_NAME, clip_forecast, date_axis, detail_labels, first_holding_index, month_axis,
    null_before_start, shift_month, stacked_month_series, stacked_tooltip_js, tick_plan,
)

ROOT = Path(__file__).resolve().parents[1]


# ---- month axis (item 1) -----------------------------------------------------------------------

def test_axis_includes_a_forecast_payment_in_the_third_calendar_month():
    actual = [f'2026-{m:02d}' for m in range(1, 11)]
    axis = month_axis(actual, ['2026-10', '2026-11', '2026-12'], date(2026, 1, 1), date(2026, 10, 3))
    assert axis[0] == '2026-01' and axis[-1] == '2026-12'          # payment on 2 Dec: forecast-only column
    assert clip_forecast({'2026-10': 1, '2026-11': 2, '2026-12': 3}, axis) == {
        '2026-10': 1, '2026-11': 2, '2026-12': 3}


def test_axis_without_a_december_payment_ends_at_november_or_october():
    actual = [f'2026-{m:02d}' for m in range(1, 11)]
    assert month_axis(actual, ['2026-10', '2026-11'], date(2026, 1, 1), date(2026, 10, 3))[-1] == '2026-11'
    assert month_axis(actual, ['2026-10'], date(2026, 1, 1), date(2026, 10, 3))[-1] == '2026-10'
    assert month_axis(actual, [], date(2026, 1, 1), date(2026, 10, 3))[-1] == '2026-10'


def test_3m_axis_starts_at_the_range_month_and_is_contiguous():
    axis = month_axis(['2026-08'], [], date(2026, 7, 3), date(2026, 10, 3))
    assert axis == ['2026-07', '2026-08', '2026-09', '2026-10']


def test_max_axis_starts_at_the_first_month_with_data():
    axis = month_axis(['2024-03', '2026-09'], ['2026-11'], None, date(2026, 10, 3))
    assert axis[0] == '2024-03' and axis[-1] == '2026-11' and len(axis) == 33


def test_month_boundaries_and_year_rollover():
    assert month_axis([], [], date(2026, 11, 20), date(2026, 12, 31))[-1] == '2026-12'
    assert month_axis([], ['2027-01'], date(2026, 11, 20), date(2026, 12, 31))[-1] == '2027-01'
    assert month_axis([], [], date(2026, 1, 1), date(2026, 1, 31)) == ['2026-01']
    assert shift_month('2026-12', 1) == '2027-01' and shift_month('2026-01', -1) == '2025-12'


def test_axis_with_no_data_and_a_late_actual_month():
    assert month_axis([], [], None, date(2026, 10, 3)) == ['2026-10']
    assert month_axis(['2026-12'], [], date(2026, 11, 1), date(2026, 10, 3))[-1] == '2026-12'


# ---- date axis (item 3) -------------------------------------------------------------------------

def _days(start, n):
    from datetime import timedelta
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def test_tick_plan_modes_follow_the_span():
    assert tick_plan(_days(date(2026, 7, 1), 90))[0] == 'week'
    assert tick_plan(_days(date(2026, 1, 1), 280))[0] == 'month'
    assert tick_plan(_days(date(2023, 10, 1), 1095))[0] == 'quarter'
    assert tick_plan(_days(date(2012, 1, 1), 4000))[0] == 'year'


def test_month_ticks_sit_on_the_first_date_of_each_month():
    dates = _days(date(2026, 1, 2), 280)
    mode, idx = tick_plan(dates)
    assert [dates[i][:7] for i in idx] == [f'2026-{m:02d}' for m in range(1, 10)] + ['2026-10']
    assert dates[idx[1]].endswith('-01')


def test_date_axis_is_horizontal_daily_and_labelled_by_js_plan():
    dates = _days(date(2026, 1, 2), 100)
    ax = date_axis(dates)
    assert ax['data'] == dates                                   # tooltips stay daily
    assert ax['axisLabel']['rotate'] == 0
    assert ax['axisLabel'][':interval'].startswith('(i) => [0,')
    assert 'Jan' in ax['axisLabel'][':formatter'] or ax['axisLabel'][':formatter'] == "(v) => v.slice(5)"


def test_every_daily_line_chart_uses_the_shared_date_axis():
    src = (ROOT / 'ba2_trade_platform' / 'ui' / 'pages' / 'overview.py').read_text(encoding='utf-8')
    assert "'rotate': 45" not in src
    assert src.count('date_axis(all_dates)') == 4


# ---- null before the position existed (item 5) -----------------------------------------------------

def test_no_line_before_the_first_holding_date():
    vals = [0.0, 0.0, 0.0, 5.0, 6.0]
    start = first_holding_index(vals)
    assert start == 3 and null_before_start(vals, start) == [None, None, None, 5.0, 6.0]
    assert first_holding_index([0, 0]) == 2 and first_holding_index([None, 0, 2]) == 2


def test_detail_labels_are_capped():
    labels = [f'L{i}' for i in range(10)]
    shown, left = detail_labels(labels)
    assert shown == labels[:MAX_DETAIL_LABELS] and left == 10 - MAX_DETAIL_LABELS
    assert detail_labels(['a']) == (['a'], 0)


# ---- stacked bars (item 2): same numbers, different layout --------------------------------------------

MONTHS = ['2026-08', '2026-09', '2026-10']
VALUES = {('2026-08', 'A'): 10.0, ('2026-08', 'B'): -4.0, ('2026-09', 'A'): -2.5, ('2026-09', 'B'): 6.25}
FORECAST = {('2026-10', 'A'): 3.0}


def _series():
    return stacked_month_series(
        MONTHS, ['A', 'B'], lambda m, lb: VALUES.get((m, lb)), lambda m, lb: FORECAST.get((m, lb)),
        lambda lb: '#111111', lambda lb: 'rgba(1,1,1,0.35)')


def test_stacked_series_keep_every_value_and_stack_by_sign():
    s = _series()
    by = {x['name']: x for x in s}
    assert by['A']['data'] == [10.0, -2.5, None] and by['B']['data'] == [-4.0, 6.25, None]
    assert all(x.get('stack') == 'labels' and x.get('stackStrategy') == 'samesign'
               for x in s if x['type'] == 'bar')


def test_total_marker_is_the_actual_sum_and_never_includes_the_forecast():
    s = _series()
    total = next(x for x in s if x['name'] == TOTAL_NAME)
    assert total['data'] == [6.0, 3.75, None]                    # Oct has only a forecast
    fc = next(x for x in s if x['name'] == 'A (forecast, estimated)')
    assert fc['data'] == [None, None, 3.0] and 'borderType' in fc['itemStyle']
    assert fc['itemStyle']['color'] == 'rgba(1,1,1,0.35)'


def test_stacked_money_matches_the_old_side_by_side_bars():
    s = _series()
    for m_i, m in enumerate(MONTHS):
        stacked = sum(x['data'][m_i] or 0 for x in s if x['type'] == 'bar' and 'forecast' not in x['name'])
        old = sum(v for (mm, _), v in VALUES.items() if mm == m)
        assert stacked == pytest.approx(old)


def test_tooltip_sorts_by_magnitude_in_dollars_or_percent():
    js = stacked_tooltip_js(False)
    assert 'Math.abs(b.value) - Math.abs(a.value)' in js and "'$'" in js
    assert "'%'" in stacked_tooltip_js(True)


# ---- the real monthly-by-label chart ----------------------------------------------------------------------

@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-chart-axes'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def test_label_chart_is_stacked_with_the_same_money_and_a_total(nicegui_client, monkeypatch):
    from nicegui import ui
    import ba2_trade_platform.ui.pages.overview as ov
    captured = []

    def fake_echart(options, **k):
        captured.append(options)
        return ui.label('chart')
    monkeypatch.setattr(ov, 'responsive_echart', fake_echart)
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    tab._scope, tab._account_ids, tab._single_account = None, [1], 1
    tab._forecast, tab._hidden_labels = {'2026-10': {'total': 3.0, 'labels': {'L1': 3.0}}}, 0
    labels = [f'LABEL_{i:02d}' for i in range(19)]
    by_label = {'2026-08': {lb: float(i + 1) for i, lb in enumerate(labels)},
                '2026-09': {lb: -float(i) for i, lb in enumerate(labels)}}
    months = month_axis(['2026-08', '2026-09'], ['2026-10'], date(2026, 8, 1), date(2026, 10, 3))
    with nicegui_client.content:
        tab._render_monthly_profit_by_label_chart(months, by_label, labels, {})
    opts = captured[0]
    bars = [s for s in opts['series'] if s['type'] == 'bar' and 'forecast' not in s['name']]
    assert len(bars) == 19 and all(s['stack'] == 'labels' for s in bars)
    for m_i, m in enumerate(months):
        want = sum(by_label.get(m, {}).values())
        got = sum(s['data'][m_i] or 0 for s in bars)
        assert got == pytest.approx(want)
    total = next(s for s in opts['series'] if s['name'] == TOTAL_NAME)
    assert total['data'][:2] == [pytest.approx(sum(by_label['2026-08'].values())),
                                 pytest.approx(sum(by_label['2026-09'].values()))]
    assert opts['xAxis']['data'] == months and months[-1] == '2026-10'
    assert ':formatter' in opts['tooltip']
    assert opts['legend']['bottom'] == 0 and opts['legend']['type'] == 'scroll'


# ---- Growth by Label flags (item 4) ----------------------------------------------------------------------------

def test_dividends_and_invested_default_off_and_saved_choices_win():
    import ba2_trade_platform.ui.pages.overview as ov
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    tab._account_ids, tab._single_account = [7], 7
    assert tab._flag('show_total', True) is True
    assert tab._flag('show_dividends', False) is False and tab._flag('show_invested', False) is False
    tab._set_flag('show_dividends', True)
    assert tab._flag('show_dividends', False) is True            # persisted per account (DB)
    other = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    other._account_ids, other._single_account = [8], 8
    assert other._flag('show_dividends', False) is False         # another account is untouched


# ---- controls (item 6) ---------------------------------------------------------------------------------------------

def test_stylesheet_has_tap_target_rules_for_the_controls():
    css = (ROOT / 'ba2_trade_platform' / 'ui' / 'static' / 'styles.css').read_text(encoding='utf-8')
    assert css.count('{') == css.count('}')
    block = css[css.index('.ba2-ctl.q-btn'):]
    assert 'min-height: 32px' in block and 'min-height: 40px' in block
    assert '@media (max-width: 639px)' in block


def test_controls_carry_the_tap_target_class():
    src = (ROOT / 'ba2_trade_platform' / 'ui' / 'pages' / 'overview.py').read_text(encoding='utf-8')
    helpers = (ROOT / 'ba2_trade_platform' / 'ui' / 'utils' / 'chart_helpers.py').read_text(encoding='utf-8')
    assert "classes('ba2-ctl')" in helpers
    assert src.count('ba2-ctl') >= 6
    assert "ui.toggle(['$', '%']" not in src
