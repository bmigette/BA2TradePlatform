"""Pure helpers of the Performance tab (ui/components/performance_view.py)."""
from datetime import datetime

from ba2_trade_platform.ui.components import performance_view as pv


def test_format_money_puts_the_sign_before_the_symbol():
    assert pv.format_money(-1.98) == '-$1.98'
    assert pv.format_money(1234.5) == '$1,234.50'
    assert pv.format_money(-0.001) == '$0.00'
    assert pv.format_money(None) == '—'


def test_profit_factor_infinity_and_none():
    assert pv.format_profit_factor(float('inf'))[0] == '∞'
    assert 'No losing trades' in pv.format_profit_factor(float('inf'))[1]
    assert pv.format_profit_factor(None)[0] == '—'
    assert pv.format_profit_factor(1.234)[0] == '1.23'


def test_sharpe_na_carries_its_reason():
    text, tip = pv.format_sharpe(None, 12)
    assert text == 'N/A' and '30' in tip and '12' in tip
    assert pv.format_sharpe(0.667, 40)[0] == '0.67'


def test_period_normalisation_and_caption():
    assert pv.normalize_period_days(90) == 90
    assert pv.normalize_period_days(True) == pv.DEFAULT_PERIOD_DAYS
    assert pv.normalize_period_days('x') == pv.DEFAULT_PERIOD_DAYS
    assert pv.normalize_period_days(14) == pv.DEFAULT_PERIOD_DAYS
    assert pv.period_caption(30) == 'Last 30 days'
    assert pv.period_caption(3650) == 'All time'
    assert pv.period_caption(365) == 'Last 1 year'


def _metrics(**pnls):
    return {n: {'total_pnl': p} for n, p in pnls.items()}


def test_order_is_best_pnl_first_then_extras_alphabetical():
    order = pv.order_experts(_metrics(b=5, a=5, c=-3), extra_names={'z', 'd', 'a'})
    assert order == ['a', 'b', 'c', 'd', 'z']


def test_colors_are_stable_per_name_and_cycle():
    order = [f'e{i}' for i in range(25)]
    colors = pv.assign_colors(order)
    assert colors['e0'] == colors['e20'] and colors['e0'] != colors['e1']
    assert pv.assign_colors(order) == colors


def test_layout_by_width_and_default():
    assert pv.choose_layout(390).phone and pv.choose_layout(639).phone
    assert not pv.choose_layout(640).phone
    assert not pv.choose_layout(None).phone


def test_bar_height_scales_with_experts_and_has_a_floor():
    lay = pv.DESKTOP_LAYOUT
    assert pv.bar_chart_height(1, lay) == lay.min_height
    assert pv.bar_chart_height(20, lay) == 20 * lay.row_px + lay.chrome_px
    assert pv.bar_chart_height(20, lay) > pv.bar_chart_height(10, lay)


def test_padded_range_leaves_room_for_labels_on_both_sides():
    lo, hi = pv.padded_range([-100, 50], 0.2)
    assert lo < -100 and hi > 50
    assert pv.padded_range([5, 10], 0.2)[0] == 0.0
    assert pv.padded_range([0, 0], 0.2) == [-1.0, 1.0]


def test_shorten_label():
    assert pv.shorten_label('abcdef', None) == 'abcdef'
    short = pv.shorten_label('goal2020-small_ED_S2top1', 20)
    assert len(short) == 20 and short.endswith('S2top1') and short.startswith('goal')


def test_shorten_labels_keeps_distinct_names_distinct():
    names = [f'goal2020-small_ED_S{i}top1' for i in range(1, 6)] + ['goal2020-small_ED_S1top2']
    out = pv.shorten_labels(names, 14)
    assert len(set(out)) == len(set(names))
    assert pv.shorten_labels(names, None) == names
    # two names whose head and tail are identical force a longer limit, never a clash
    tricky = ['aaaaaaaaaa-X-bbbbbbbbbb', 'aaaaaaaaaa-Y-bbbbbbbbbb']
    out = pv.shorten_labels(tricky, 10)
    assert len(set(out)) == 2 and all(len(o) <= len(t) for o, t in zip(out, tricky))
    assert pv.shorten_labels([], 10) == []


def test_legend_rows_grow_with_names():
    names = [f'goal2020-small_RAT_S1top{i}' for i in range(10)]
    assert pv.legend_rows([], 700) == 0
    assert pv.legend_rows(names, 330) > pv.legend_rows(names, 700) >= 1


def test_empty_state_explains_what_is_counted():
    head, detail = pv.empty_state(True, False, False, 30)
    assert 'no expert instances' in head and 'manually' in detail
    head, detail = pv.empty_state(True, True, True, 30)
    assert '30 days' in head and 'clear the expert filter' in detail
    assert 'expert filter' not in pv.empty_state(False, True, False, 30)[1]


def test_hbar_figure_has_headroom_full_names_and_unclipped_labels():
    names = ['goal2020-small_RAT_S1top1', 'b']
    colors = pv.assign_colors(names)
    fig = pv.hbar_figure(names, [-120.0, 60.0], colors, pv.PHONE_LAYOUT, 'money')
    bar = fig.data[0]
    assert bar.orientation == 'h' and bar.cliponaxis is False
    assert list(bar.y) == names                        # full name stays the hover value
    assert '…' in fig.layout.yaxis.ticktext[0] and fig.layout.yaxis.ticktext[0].endswith('S1top1')
    lo, hi = fig.layout.xaxis.range
    assert lo < -120 and hi > 60
    assert fig.layout.yaxis.autorange == 'reversed'
    assert list(bar.marker.color) == [colors[n] for n in names]


def test_win_loss_figure_is_a_stacked_bar_with_rates():
    fig = pv.win_loss_figure(['a', 'b'], [3, 0], [1, 0], pv.DESKTOP_LAYOUT)
    assert fig.layout.barmode == 'stack' and len(fig.data) == 2
    assert [a.text for a in fig.layout.annotations] == ['75%', '—']


def test_monthly_figure_legend_is_below_and_margin_fits_it():
    names = [f'goal2020-small_RAT_S1top{i}' for i in range(10)]
    series = {n: [(datetime(2026, 2, 1), 1.0), (datetime(2026, 3, 1), 2.0)] for n in names}
    colors = pv.assign_colors(names)
    fig = pv.monthly_line_figure(series, names, colors, pv.PHONE_LAYOUT, 'money')
    assert fig.layout.legend.orientation == 'h' and fig.layout.legend.y < 0
    rows = pv.legend_rows(names, pv.PHONE_LAYOUT.legend_width_px)
    assert fig.layout.margin.b >= 40 + rows * 18
    assert fig.layout.title.text is None


def test_monthly_series_drawdown_walks_the_cumulative_curve():
    data = {'2026-01': {'a': {'pnl': 10.0, 'count': 1}, 'n': {'pnl': -5.0, 'count': 1}},
            '2026-02': {'a': {'pnl': -5.0, 'count': 2}}}
    profit, count, dd = pv.monthly_series(data)
    assert [v for _d, v in dd['a']] == [0.0, 5.0]       # dollars below the running peak
    assert [v for _d, v in dd['n']] == [5.0, 5.0]       # peak starts at 0, as the $ metric
    assert [v for _d, v in count['a']] == [1, 2]
    assert [v for _d, v in profit['n']] == [-5.0, 0]


def test_every_hover_shows_the_full_name_not_the_shortened_tick():
    names = ['goal2020-sen_S6_notional_top1', 'goal2020-small_ED_S2top1']
    colors = pv.assign_colors(names)
    hb = pv.hbar_figure(names, [20.94, -3.0], colors, pv.PHONE_LAYOUT, 'money').data[0]
    wl = pv.win_loss_figure(names, [3, 1], [1, 2], pv.PHONE_LAYOUT)
    series = {n: [(datetime(2026, 2, 1), 1.0)] for n in names}
    ml = pv.monthly_line_figure(series, names, colors, pv.PHONE_LAYOUT, 'money')
    for trace in [hb, *wl.data, *ml.data]:
        assert '%{customdata}' in trace.hovertemplate and '%{y}<' not in trace.hovertemplate
    assert list(hb.customdata) == names and list(wl.data[0].customdata) == names
    assert [t.customdata[0] for t in ml.data] == names


def test_modebar_hidden_on_phone_only():
    assert pv.plot_config(pv.PHONE_LAYOUT)['displayModeBar'] is False
    assert pv.plot_config(pv.DESKTOP_LAYOUT)['displayModeBar'] is True
    assert pv.plot_config(pv.PHONE_LAYOUT)['responsive'] is True
