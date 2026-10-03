"""Review fixes for the Overview label picker / range / forecast branch.

Written failing-first: forecast share counts with trades between payments, per-account
forecast, per-position P&L / Invested independent of the range, cached broker data,
YTD in early January, effective range start, hostile stored values.
"""
from datetime import date, datetime, timedelta

import pytest

from ba2_trade_platform.ui.utils.dividend_forecast import add_months
from ba2_trade_platform.ui.utils.overview_range import resolve_range_start


def _tab():
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    return AccountGrowthTab.__new__(AccountGrowthTab)


def _rows(sym, amt, aid=1, n=5, day=15, drip=None, shift_days=0):
    t = date.today()
    d = date(t.year, t.month, day)
    if d >= t:
        d = add_months(d, -1)
    return [{'symbol': sym, 'account_id': aid, 'amount': amt, 'drip_quantity': drip,
             'date': datetime(*add_months(d, -k).timetuple()[:3]) + timedelta(days=shift_days)}
            for k in range(n)]


def _totals(out):
    return {m: round(v['total'], 2) for m, v in out.items()}


# ---- 1: forecast share counts -------------------------------------------------------

def test_sold_after_the_last_payment_forecasts_todays_small_holding():
    divs = _rows('XYZ', 50.0)                                    # 100 sh x $0.50
    sell = {'symbol': 'XYZ', 'account_id': 1, 'side': 'SELL', 'qty': 90, 'price': 20,
            'date': max(r['date'] for r in divs) + timedelta(days=3)}
    out = _tab()._compute_dividend_forecast(divs, [sell], {(1, 'XYZ'): 10.0})
    assert out and set(_totals(out).values()) == {5.0}


def test_bought_after_the_last_payment_forecasts_todays_large_holding():
    divs = _rows('XYZ', 5.0)                                     # 10 sh x $0.50
    buy = {'symbol': 'XYZ', 'account_id': 1, 'side': 'BUY', 'qty': 90, 'price': 20,
           'date': max(r['date'] for r in divs) + timedelta(days=3)}
    out = _tab()._compute_dividend_forecast(divs, [buy], {(1, 'XYZ'): 100.0})
    assert out and set(_totals(out).values()) == {50.0}


def test_a_buy_between_two_older_payments_is_reversed_correctly():
    divs = sorted(_rows('XYZ', 0.0), key=lambda r: r['date'])
    for i, r in enumerate(divs):
        r['amount'] = 5.0 if i < 3 else 50.0                     # 10 sh then 100 sh, $0.50/sh
    buy = {'symbol': 'XYZ', 'account_id': 1, 'side': 'BUY', 'qty': 90, 'price': 20,
           'date': divs[3]['date'] - timedelta(days=5)}
    out = _tab()._compute_dividend_forecast(divs, [buy], {(1, 'XYZ'): 100.0})
    assert set(_totals(out).values()) == {50.0}


def test_drip_shares_are_not_part_of_the_paying_quantity():
    out = _tab()._compute_dividend_forecast(_rows('XYZ', 50.0, drip=2.5), [], {(1, 'XYZ'): 112.5})
    assert set(_totals(out).values()) == {51.14}


# ---- 7: per-account forecast ----------------------------------------------------------

def test_two_accounts_booking_a_day_apart_still_forecast_and_sum():
    divs = _rows('XYZ', 50.0, aid=1) + _rows('XYZ', 25.0, aid=2, shift_days=1)
    out = _tab()._compute_dividend_forecast(divs, [], {(1, 'XYZ'): 100.0, (2, 'XYZ'): 50.0})
    assert out and set(_totals(out).values()) == {75.0}


def test_short_positions_get_no_forecast():
    assert _tab()._compute_dividend_forecast(_rows('XYZ', 50.0), [], {(1, 'XYZ'): -100.0}) == {}


# ---- 3: per-position P&L / Invested with a pre-range buy ---------------------------------

def _position_chart(range_key, today=date(2026, 10, 3)):
    from unittest.mock import MagicMock
    import ba2_trade_platform.ui.pages.overview as ov
    tab = _tab()
    prices, d = {}, date(2025, 6, 2)
    while d <= date(2026, 10, 2):
        if d.weekday() < 5:
            prices[d.isoformat()] = 60.0
        d += timedelta(days=1)
    trades = [{'symbol': 'XYZ', 'side': 'BUY', 'qty': 100, 'price': 50.0, 'date': datetime(2025, 6, 2)}]
    divs = [{'symbol': 'XYZ', 'amount': 60.0, 'drip_quantity': 1, 'drip_price': 60.0,
             'date': datetime(2026, 3, 16)}]
    tab._range = range_key
    tab._range_start = resolve_range_start(range_key, today)
    orig_ui, orig_re = ov.ui, ov.responsive_echart
    ov.ui, ov.responsive_echart = MagicMock(), (lambda o, **k: MagicMock())
    try:
        opts = tab._render_position_growth_chart_from_data(
            'XYZ', divs, prices, 101, filled_trades=trades, avg_entry_price=50.5)
    finally:
        ov.ui, ov.responsive_echart = orig_ui, orig_re
    ser = {s['name']: [v for v in s['data'] if v is not None] for s in opts['series']}
    return ser['P&L %'][-1], ser['Invested'][-1]


def test_per_position_pnl_and_invested_agree_across_ranges():
    mx = _position_chart('Max')
    assert mx == pytest.approx((19.76, 5060.0), abs=0.01)
    assert _position_chart('1y') == pytest.approx(mx)
    assert _position_chart('YTD') == pytest.approx(mx)


# ---- 12: hostile stored values -----------------------------------------------------------

def test_pick_single_ignores_unhashable_and_non_string_stored_values():
    from ba2_trade_platform.ui.utils.overview_label_scope import pick_single
    for bad in (['A'], {'a': 1}, 5, None):
        assert pick_single([bad, 'B'], ['A', 'B'], 'A') == 'B'
        assert pick_single([bad], ['A', 'B'], 'A') == 'A'


# ---- 14: YTD in early January --------------------------------------------------------------

def test_ytd_is_never_blank_on_the_first_days_of_january():
    assert resolve_range_start('YTD', date(2026, 1, 1)) < date(2026, 1, 1)
    assert resolve_range_start('YTD', date(2026, 1, 3)) < date(2026, 1, 1)
    assert resolve_range_start('YTD', date(2026, 1, 4)) == date(2026, 1, 1)
    assert resolve_range_start('YTD', date(2026, 6, 1)) == date(2026, 1, 1)


# ---- 5: effective start --------------------------------------------------------------------

def test_effective_start_never_precedes_the_accounts_first_activity():
    from ba2_trade_platform.ui.utils.overview_range import effective_range_start
    first = date(2024, 5, 7)
    assert effective_range_start(None, first) == first                   # Max
    assert effective_range_start(date(2023, 1, 1), first) == first       # 3y, young account
    assert effective_range_start(date(2025, 1, 1), first) == date(2025, 1, 1)
    assert effective_range_start(None, None) is None
    assert effective_range_start(date(2025, 1, 1), None) == date(2025, 1, 1)


# ---- 4: broker data fetched once per page ------------------------------------------------------

@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-overview-review-fixes'), request=None)
    yield client
    client.remove_elements(client.elements.values())


class _CountingAccount:
    def __init__(self):
        self.calls = {}

    def _c(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def get_balance_history(self, start_date=None, end_date=None):
        self._c('balance')
        return [{'date': datetime.now() - timedelta(days=d), 'net_liquidating_value': 1000.0,
                 'cash_balance': 10.0} for d in range(30, 0, -1)]

    def get_dividends(self, **k):
        self._c('dividends')
        return []

    def get_filled_trades(self, **k):
        self._c('trades')
        return []

    def get_positions(self):
        from types import SimpleNamespace
        self._c('positions')
        return [SimpleNamespace(symbol='AAA', qty=1, cost_basis=10.0, avg_entry_price=10.0)]


def test_changing_the_range_does_not_refetch_broker_data(nicegui_client, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from nicegui import ui
    import yfinance
    import ba2_trade_platform.ui.pages.overview as ov
    acct = _CountingAccount()
    monkeypatch.setattr(ov, 'get_all_instances', lambda m: [SimpleNamespace(id=1, name='A')])
    monkeypatch.setattr(ov, 'get_account_instance_from_id', lambda i: acct)
    yf_calls = []
    monkeypatch.setattr(yfinance, 'download', lambda *a, **k: yf_calls.append(k.get('period')))
    monkeypatch.setattr(ov, '_extract_yf_close_prices', lambda h, s: {'2026-09-01': 1.0})
    for name in ('_render_monthly_realized_income_chart', '_render_monthly_profit_by_label_chart',
                 '_render_total_growth_chart', '_render_growth_by_label_charts',
                 '_render_growth_by_position_in_label_charts', '_render_per_position_section',
                 '_render_dividend_history_table', '_render_scope_controls'):
        monkeypatch.setattr(ov.AccountGrowthTab, name, lambda *a, **k: None)
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    tab._scope, tab._account_ids, tab._single_account = None, [], None
    tab._forecast, tab._gen = {}, 0
    tab._init_data_cache()

    async def go():
        with nicegui_client.content:
            for rk in ('YTD', '3m', '6m', '1y'):
                tab._set_range(rk)
                await tab._load_growth_data(ui.label('x'), ui.column(), 1)
    asyncio.run(go())
    assert acct.calls == {'balance': 1, 'dividends': 1, 'trades': 1, 'positions': 1}, acct.calls
    assert yf_calls == ['1y'], yf_calls     # 3m / 6m / 1y need no longer period than YTD's 1y


def test_history_capped_only_when_the_broker_returned_less_than_the_range_wants():
    from ba2_trade_platform.ui.utils.overview_range import history_capped
    act = date(2022, 1, 1)
    assert history_capped(None, date(2025, 10, 1), act)                      # Max, 1y of history
    assert history_capped(date(2023, 10, 3), date(2025, 10, 1), act)         # 3y, 1y of history
    assert not history_capped(date(2023, 10, 3), date(2023, 10, 5), act)
    assert not history_capped(None, date(2025, 10, 1), date(2025, 9, 20))    # young account
    assert not history_capped(None, None, act)


def test_a_failed_setting_write_is_surfaced_to_the_user(monkeypatch):
    import ba2_trade_platform.ui.pages.overview as ov
    notes = []
    monkeypatch.setattr(ov, 'write_overview_setting', lambda k, v: False)
    monkeypatch.setattr(ov.ui, 'notify', lambda *a, **k: notes.append((a, k)))
    assert ov._save_setting('overview_x', ['A']) is False
    assert notes and notes[0][1]['type'] == 'warning'
    monkeypatch.setattr(ov, 'write_overview_setting', lambda k, v: True)
    notes.clear()
    assert ov._save_setting('overview_x', ['A']) is True and not notes


def test_no_ui_caller_writes_settings_without_the_notifying_helper():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'ba2_trade_platform' / 'ui' / 'pages'
           / 'overview.py').read_text(encoding='utf-8')
    assert src.count('write_overview_setting(') == 1          # only inside _save_setting
