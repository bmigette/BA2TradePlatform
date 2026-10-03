"""Monthly-by-label chart with overlapping labels (live bug: stacks double-counted, Total was a segment sum)."""
from datetime import datetime

import pytest

from ba2_trade_platform.ui.utils.chart_axes import (
    TOTAL_NAME, shown_label_totals, stacked_month_series, stacked_tooltip_js,
)

M = ['2026-08', '2026-09']
BY_SYMBOL = {'2026-08': {'AAA': 10.0, 'BBB': 5.0, 'CCC': -3.0},
             '2026-09': {'AAA': 4.0, 'BBB': 6.0, 'CCC': 1.5}}
VALUES = {('2026-08', 'A'): 10.0, ('2026-08', 'B'): -4.0, ('2026-09', 'A'): -2.5, ('2026-09', 'B'): 6.25}
FORECAST = {('2026-10', 'A'): 3.0}
MONTHS = ['2026-08', '2026-09', '2026-10']


def test_overlapping_labels_total_counts_each_symbol_once():
    # AAA carries BOTH shown labels (an index label + an expert label); CCC is in neither
    labels_of = {'AAA': ['SP500', 'expert-8'], 'BBB': ['SP500'], 'CCC': ['other']}
    r = shown_label_totals(M, ['SP500', 'expert-8'], BY_SYMBOL, labels_of)
    assert r['overlap'] is True
    assert r['totals'] == [15.0, 10.0]                      # AAA once + BBB, NOT 10+10+5
    assert r['not_shown'] == [-3.0, 1.5]
    for i, m in enumerate(M):                               # account total == shown + not shown
        assert r['totals'][i] + r['not_shown'][i] == pytest.approx(sum(BY_SYMBOL[m].values()))


def test_disjoint_labels_have_no_overlap_and_total_is_the_segment_sum():
    labels_of = {'AAA': ['L1'], 'BBB': ['L2'], 'CCC': ['L3']}
    r = shown_label_totals(M, ['L1', 'L2'], BY_SYMBOL, labels_of)
    assert r['overlap'] is False and r['totals'] == [15.0, 10.0]
    seg = {m: {'L1': BY_SYMBOL[m]['AAA'], 'L2': BY_SYMBOL[m]['BBB']} for m in M}
    assert [sum(seg[m].values()) for m in M] == r['totals']


def test_overlap_only_counts_labels_that_are_shown():
    labels_of = {'AAA': ['SP500', 'expert-8'], 'BBB': ['SP500'], 'CCC': ['other']}
    r = shown_label_totals(M, ['SP500'], BY_SYMBOL, labels_of)       # expert-8 hidden: no overlap
    assert r['overlap'] is False and r['totals'] == [15.0, 10.0]


def test_unlabeled_symbols_and_empty_months():
    r = shown_label_totals(['2026-07', '2026-08'], ['Unlabeled'], {'2026-08': {'ZZZ': 2.0}}, {})
    assert r['totals'] == [None, 2.0] and r['not_shown'] == [None, 0.0]


def test_grouped_series_when_not_stacked_and_marker_override():
    s = stacked_month_series(MONTHS, ['A', 'B'], lambda m, lb: VALUES.get((m, lb)),
                             lambda m, lb: FORECAST.get((m, lb)), lambda lb: '#111',
                             lambda lb: 'rgba(1,1,1,.3)', stacked=False, totals=[1.0, 2.0, None])
    bars = [x for x in s if x['type'] == 'bar']
    assert {x['stack'] for x in bars if x['name'] in ('A', 'B')} == {'A', 'B'}     # one stack PER label
    assert not any('stackStrategy' in x for x in bars)
    assert next(x for x in s if x['name'] == 'A (forecast, estimated)')['stack'] == 'A'
    assert next(x for x in s if x['name'] == TOTAL_NAME)['data'] == [1.0, 2.0, None]


def test_tooltip_carries_the_not_shown_line_and_the_total_name():
    js = stacked_tooltip_js(False, [None, -3.0])
    assert 'Not shown (other labels)' in js and TOTAL_NAME in js and '[null, -3.0]' in js


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-label-overlap'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _label_chart(nicegui_client, monkeypatch, by_label, labels_of, labels):
    from nicegui import ui
    import ba2_trade_platform.ui.pages.overview as ov
    captured = []

    def fake_echart(options, **k):
        captured.append(options)
        return ui.label('chart')
    monkeypatch.setattr(ov, 'responsive_echart', fake_echart)
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    tab._scope, tab._account_ids, tab._single_account = None, [1], 1
    tab._forecast, tab._hidden_labels = {}, 0
    tab._monthly_symbol_detail = {'by_symbol': BY_SYMBOL, 'labels_of': labels_of}
    with nicegui_client.content:
        tab._render_monthly_profit_by_label_chart(M, by_label, labels, {})
        texts = [e.text for e in nicegui_client.elements.values() if type(e).__name__ == 'Label']
    return captured[0], texts


def test_chart_uses_grouped_bars_and_the_distinct_total_when_labels_overlap(nicegui_client, monkeypatch):
    labels_of = {'AAA': ['SP500', 'expert-8'], 'BBB': ['SP500'], 'CCC': ['other']}
    by_label = {m: {'SP500': BY_SYMBOL[m]['AAA'] + BY_SYMBOL[m]['BBB'], 'expert-8': BY_SYMBOL[m]['AAA'],
                    'other': BY_SYMBOL[m]['CCC']} for m in M}
    opts, texts = _label_chart(nicegui_client, monkeypatch, by_label, labels_of, ['SP500', 'expert-8', 'other'])
    # all three labels are shown by default: AAA overlaps (SP500 + expert-8) -> grouped
    bars = [s for s in opts['series'] if s['type'] == 'bar']
    assert {s['stack'] for s in bars} == {'SP500', 'expert-8', 'other'}           # no shared stack
    total = next(s for s in opts['series'] if s['name'] == TOTAL_NAME)
    assert total['data'] == [12.0, 11.5]                                          # distinct symbols incl. CCC
    assert TOTAL_NAME in opts['legend']['data']
    assert any('Labels overlap' in t for t in texts)


def test_not_shown_remainder_is_the_account_total_minus_the_shown_union(nicegui_client, monkeypatch):
    labels_of = {'AAA': ['SP500', 'expert-8'], 'BBB': ['SP500'], 'CCC': ['other']}
    by_label = {m: {'SP500': BY_SYMBOL[m]['AAA'] + BY_SYMBOL[m]['BBB'], 'expert-8': BY_SYMBOL[m]['AAA'],
                    'other': BY_SYMBOL[m]['CCC']} for m in M}
    # the page's persisted selection hides 'other': simulate it through the saved choice
    from ba2_trade_platform.ui.utils.overview_label_scope import chart_key, write_overview_setting
    write_overview_setting(chart_key('monthly_profit', 1), ['SP500', 'expert-8'])
    opts, texts = _label_chart(nicegui_client, monkeypatch, by_label, labels_of, ['SP500', 'expert-8', 'other'])
    total = next(s for s in opts['series'] if s['name'] == TOTAL_NAME)
    assert total['data'] == [15.0, 10.0]
    assert '[-3.0, 1.5]' in opts['tooltip'][':formatter']
    for i, m in enumerate(M):
        assert total['data'][i] + [-3.0, 1.5][i] == pytest.approx(sum(BY_SYMBOL[m].values()))


def test_chart_stacks_when_the_shown_labels_are_disjoint(nicegui_client, monkeypatch):
    labels_of = {'AAA': ['L1'], 'BBB': ['L2'], 'CCC': ['L3']}
    by_label = {m: {'L1': BY_SYMBOL[m]['AAA'], 'L2': BY_SYMBOL[m]['BBB'], 'L3': BY_SYMBOL[m]['CCC']} for m in M}
    opts, texts = _label_chart(nicegui_client, monkeypatch, by_label, labels_of, ['L1', 'L2', 'L3'])
    bars = [s for s in opts['series'] if s['type'] == 'bar']
    assert {s['stack'] for s in bars} == {'labels'}
    total = next(s for s in opts['series'] if s['name'] == TOTAL_NAME)
    seg_sum = [sum(s['data'][i] or 0 for s in bars) for i in range(2)]
    assert total['data'] == seg_sum == [12.0, 11.5]
    assert any('disjoint' in t for t in texts)


def test_compute_monthly_realized_records_the_per_symbol_detail(monkeypatch):
    import ba2_trade_platform.ui.pages.overview as ov
    monkeypatch.setattr(ov, 'get_labels_by_symbol', lambda syms: {'AAA': ['SP500', 'expert-8'], 'BBB': []})
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    trades = [{'symbol': 'AAA', 'side': 'BUY', 'qty': 1, 'price': 10, 'date': datetime(2026, 8, 1)},
              {'symbol': 'AAA', 'side': 'SELL', 'qty': 1, 'price': 14, 'date': datetime(2026, 9, 2)}]
    divs = [{'symbol': 'BBB', 'amount': 2.0, 'date': datetime(2026, 9, 5)}]
    months, income, by_label, labels = tab._compute_monthly_realized(trades, divs)
    det = tab._monthly_symbol_detail
    assert det['by_symbol']['2026-09'] == {'AAA': 4.0, 'BBB': 2.0}
    assert det['labels_of'] == {'AAA': ['SP500', 'expert-8'], 'BBB': ['Unlabeled']}
    assert by_label['2026-09']['SP500'] == 4.0 and by_label['2026-09']['expert-8'] == 4.0
    assert income['2026-09']['pnl'] + income['2026-09']['div'] == 6.0
