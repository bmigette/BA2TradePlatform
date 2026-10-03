"""Overview time range (one pure resolver) and the dividend forecast rule."""
from pathlib import Path
from datetime import date

import pytest

from ba2_trade_platform.ui.utils.dividend_forecast import add_months, forecast_dividends
from ba2_trade_platform.ui.utils.overview_range import (
    DEFAULT_RANGE, RANGE_OPTIONS, date_in_range, filter_dates, filter_months,
    normalize_range, read_range, resolve_range_start, write_range, yf_period_for,
)


# ---- range ------------------------------------------------------------------

def test_options_and_default():
    assert RANGE_OPTIONS == ['3m', '6m', 'YTD', '1y', '3y', 'Max']
    assert DEFAULT_RANGE == 'YTD'


def test_ytd_boundary_is_jan_first():
    assert resolve_range_start('YTD', date(2026, 1, 1)) == date(2026, 1, 1)
    assert resolve_range_start('YTD', date(2026, 12, 31)) == date(2026, 1, 1)


def test_max_has_no_start():
    assert resolve_range_start('Max', date(2026, 10, 3)) is None


def test_month_offsets_and_month_end_clamp():
    assert resolve_range_start('3m', date(2026, 10, 3)) == date(2026, 7, 3)
    assert resolve_range_start('6m', date(2026, 8, 31)) == date(2026, 2, 28)
    assert resolve_range_start('6m', date(2028, 8, 31)) == date(2028, 2, 29)   # leap year
    assert resolve_range_start('3m', date(2026, 5, 31)) == date(2026, 2, 28)
    assert resolve_range_start('3m', date(2026, 1, 15)) == date(2025, 10, 15)  # across a year


def test_year_offsets_and_leap_day():
    assert resolve_range_start('1y', date(2028, 2, 29)) == date(2027, 2, 28)
    assert resolve_range_start('3y', date(2028, 2, 29)) == date(2025, 2, 28)
    assert resolve_range_start('1y', date(2026, 10, 3)) == date(2025, 10, 3)


def test_unknown_range_raises_and_normalize_defaults():
    with pytest.raises(ValueError):
        resolve_range_start('2w', date(2026, 1, 1))
    assert normalize_range('2w') == 'YTD' and normalize_range(None) == 'YTD'
    assert normalize_range('1y') == '1y'


def test_filters():
    start = date(2026, 3, 15)
    assert date_in_range('2026-03-15', start) and not date_in_range('2026-03-14', start)
    assert date_in_range(date(2026, 4, 1), start) and not date_in_range('garbage', start)
    assert date_in_range('1999-01-01', None)
    assert filter_dates(['2026-03-14', '2026-03-15', '2026-04-01'], start) == ['2026-03-15', '2026-04-01']
    assert filter_months(['2026-02', '2026-03', '2026-04'], start) == ['2026-03', '2026-04']
    assert filter_months(['2026-02'], None) == ['2026-02']


def test_yf_period_covers_every_range():
    assert {k: yf_period_for(k) for k in RANGE_OPTIONS} == {
        '3m': '6mo', '6m': '6mo', 'YTD': '1y', '1y': '1y', '3y': '3y', 'Max': 'max'}


def test_range_persists_in_db():
    assert read_range() == 'YTD'
    write_range('3y')
    assert read_range() == '3y'


# ---- forecast ---------------------------------------------------------------

TODAY = date(2026, 10, 3)


def _monthly(last=date(2026, 9, 15), n=6, amt=0.10):
    d, out = last, []
    for _ in range(n):
        out.append((d, amt))
        d = add_months(d, -1)
    return out


def test_monthly_payer_gets_two_payments_times_qty():
    out = forecast_dividends(_monthly(), 100, TODAY)
    assert [o['date'] for o in out] == [date(2026, 10, 15), date(2026, 11, 15)]
    assert [o['amount'] for o in out] == [10.0, 10.0]


def test_horizon_edge_is_inclusive():
    out = forecast_dividends(_monthly(last=date(2026, 9, 3)), 10, TODAY)   # Oct 3 = today
    assert [o['date'] for o in out] == [date(2026, 10, 3), date(2026, 11, 3), date(2026, 12, 3)]


def test_weekly_payer_has_several_payments():
    hist = [(date(2026, 9, 29) - __import__('datetime').timedelta(days=7 * i), 0.5) for i in range(6)]
    out = forecast_dividends(hist, 2, TODAY)
    assert len(out) >= 8 and out[0]['date'] == date(2026, 10, 6)
    assert all(o['amount'] == 1.0 for o in out)


def test_quarterly_payer_beyond_horizon_gets_nothing():
    hist = [(date(2026, 9, 25), 0.5), (date(2026, 6, 25), 0.5), (date(2026, 3, 25), 0.5)]
    assert forecast_dividends(hist, 10, TODAY) == []          # next 25 Dec > 3 Dec


def test_quarterly_payer_inside_horizon():
    hist = [(date(2026, 7, 20), 0.5), (date(2026, 4, 20), 0.5), (date(2026, 1, 20), 0.5)]
    out = forecast_dividends(hist, 10, TODAY)
    assert [o['date'] for o in out] == [date(2026, 10, 20)] and out[0]['amount'] == 5.0


def test_overdue_within_one_period_is_placed_today_and_stale_is_dropped():
    hist = [(date(2026, 8, 20), 0.2), (date(2026, 7, 20), 0.2), (date(2026, 6, 20), 0.2)]
    out = forecast_dividends(hist, 10, TODAY)                  # due 20 Sep, 13 days late
    assert out[0]['date'] == TODAY
    assert forecast_dividends(_monthly(last=date(2026, 6, 1)), 10, TODAY) == []   # long silent


def test_variable_payer_uses_mean_of_last_three_regular_uses_last():
    var = [(date(2026, 9, 15), 0.30), (date(2026, 8, 15), 0.10), (date(2026, 7, 15), 0.20)]
    assert forecast_dividends(var, 10, TODAY)[0]['per_share'] == pytest.approx(0.2)
    flat = [(date(2026, 9, 15), 0.105), (date(2026, 8, 15), 0.10), (date(2026, 7, 15), 0.10)]
    assert forecast_dividends(flat, 10, TODAY)[0]['per_share'] == pytest.approx(0.105)


def test_no_forecast_without_history_or_cadence_or_qty():
    assert forecast_dividends([], 10, TODAY) == []
    assert forecast_dividends([(date(2026, 9, 15), 0.1)], 10, TODAY) == []     # one payment
    irregular = [(date(2026, 9, 15), 0.1), (date(2026, 8, 1), 0.1), (date(2026, 7, 28), 0.1)]
    assert forecast_dividends(irregular, 10, TODAY) == []
    assert forecast_dividends(_monthly(), 0, TODAY) == []
    assert forecast_dividends(_monthly(), None, TODAY) == []


def test_same_day_payments_are_summed_and_month_end_does_not_drift():
    hist = [(date(2026, 9, 30), 0.1), (date(2026, 9, 30), 0.1), (date(2026, 8, 31), 0.2),
            (date(2026, 7, 31), 0.2)]
    out = forecast_dividends(hist, 10, date(2026, 10, 3), months=3)
    assert out[0]['date'] == date(2026, 10, 30) and out[0]['per_share'] == pytest.approx(0.2)
    assert out[1]['date'] == date(2026, 11, 30)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


# ---- forecast aggregation (page) + responsive options ---------------------------

def test_dividend_forecast_aggregates_by_month_and_label_from_page_rows():
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import Instrument
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    add_instance(Instrument(name='AAA', labels=['inc', 'tech']))
    add_instance(Instrument(name='BBB', labels=[]))
    today = datetime.now()

    def rows(sym, amt):
        # monthly payer: total payment 'amt' on 10 shares held throughout
        return [{'symbol': sym, 'account_id': 1, 'amount': amt, 'drip_quantity': None,
                 'date': today - timedelta(days=30 * k + 5)} for k in range(5)]

    tab = AccountGrowthTab.__new__(AccountGrowthTab)
    divs = rows('AAA', 5.0) + rows('BBB', 2.0) + rows('GONE', 9.0)
    out = tab._compute_dividend_forecast(divs, [], {(1, 'AAA'): 20.0, (1, 'BBB'): 10.0})
    assert out, 'a monthly payer must produce forecast months'
    total = sum(m['total'] for m in out.values())
    # AAA: 5 per 20 shares-on-date... qty walks back from 20 with no trades -> 20 shares
    # => 0.25/share * 20 held = 5.00 per payment; BBB 2.0 per payment; GONE not held.
    n_payments = round(total / 7.0)
    assert total == pytest.approx(7.0 * n_payments) and n_payments >= 1
    labels = {lb for m in out.values() for lb in m['labels']}
    assert labels == {'inc', 'tech', 'Unlabeled'}


def test_responsive_options_wrap_in_media_rule_with_bottom_scroll_legend():
    from ba2_trade_platform.ui.utils.chart_helpers import responsive_chart_options
    opts = {'legend': {'data': ['a']}, 'grid': {'top': 80}, 'series': []}
    out = responsive_chart_options(opts)
    assert out['baseOption'] is opts
    narrow = out['media'][0]
    assert narrow['query']['maxWidth'] == 520
    assert narrow['option']['legend']['type'] == 'scroll' and narrow['option']['legend']['top'] == 'bottom'
    assert narrow['option']['grid']['top'] == 24
    assert responsive_chart_options(out) is out            # idempotent
    no_legend = responsive_chart_options({'series': []})
    assert 'legend' not in no_legend['media'][0]['option']


def test_every_echart_on_the_overview_goes_through_the_responsive_wrapper():
    src = (Path(__file__).resolve().parents[1] / 'ba2_trade_platform' / 'ui' / 'pages' / 'overview.py'
           ).read_text(encoding='utf-8')
    assert 'ui.echart(' not in src
    assert src.count('responsive_echart(') >= 8
