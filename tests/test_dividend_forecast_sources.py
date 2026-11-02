"""Dividend forecast sources: broker metadata > declared provider history > account history."""
import asyncio
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from ba2_trade_platform.ui.utils.dividend_forecast import (
    add_months, broker_forecast, forecast_dividends, forecast_dividends_ex, net_ratio,
    parse_provider_history,
)

TODAY = date(2026, 10, 3)


def monthly(day=15, n=6, amt=0.25, last=date(2026, 9, 15)):
    return [(add_months(last, -k), amt) for k in range(n)][::-1]


# ---- provider (declared) history ---------------------------------------------------------------

def test_provider_monthly_weekly_and_quarterly_beyond_the_horizon():
    ev, info = forecast_dividends_ex(monthly(), 10, TODAY, specials=True)
    assert info['status'] == 'ok' and [e['date'] for e in ev] == [date(2026, 10, 15), date(2026, 11, 15)]
    assert [e['amount'] for e in ev] == [2.5, 2.5]
    weekly = [(date(2026, 9, 29) - timedelta(days=7 * i), 0.1) for i in range(8)]
    ev, info = forecast_dividends_ex(weekly, 10, TODAY, specials=True)
    assert len(ev) >= 8
    quarterly = [(date(2026, 9, 25), 0.5), (date(2026, 6, 25), 0.5), (date(2026, 3, 25), 0.5)]
    ev, info = forecast_dividends_ex(quarterly, 10, TODAY, specials=True)
    assert ev == [] and info['status'] == 'ok'          # a cadence exists; the next one is beyond: CORRECT


def test_a_special_payment_is_excluded_from_amount_and_cadence():
    hist = monthly(amt=0.20)
    hist.append((date(2026, 8, 25), 0.90))               # special, 4.5x the regular, regular ones after it
    hist.sort()
    ev, info = forecast_dividends_ex(hist, 10, TODAY, specials=True)
    assert info['per_share'] == pytest.approx(0.20) and ev[0]['per_share'] == pytest.approx(0.20)


def test_main_like_monthly_regular_with_quarterly_supplements_projects_the_monthly_cadence():
    hist = []
    for k, m in enumerate(range(1, 10)):
        hist.append((date(2026, m, 15), 0.25))
        if m in (3, 6, 9):
            hist.append((date(2026, m, 25), 0.30))       # supplemental, a different date
    ev, info = forecast_dividends_ex(sorted(hist), 10, TODAY, specials=True)
    assert info['cadence'] == ('months', 1)
    assert 0.25 <= info['per_share'] <= 0.30 and len(ev) == 2


def test_declared_future_payments_are_included_with_their_own_amount():
    hist = monthly() + [(date(2026, 10, 15), 0.31)]       # already declared, dated after today
    ev, info = forecast_dividends_ex(hist, 10, TODAY, specials=True, include_declared=True)
    assert ev[0]['date'] == date(2026, 10, 15) and ev[0]['per_share'] == pytest.approx(0.31)
    assert [e['date'] for e in ev][1] == date(2026, 11, 15)


def test_parse_provider_history_prefers_payment_dates_and_falls_back_to_ex_dates():
    payload = {'historical': [
        {'date': '2026-09-10', 'dividend': 0.2, 'paymentDate': '2026-09-25'},
        {'date': '2026-08-10', 'dividend': 0.2, 'paymentDate': '2026-08-25'}]}
    hist, kind = parse_provider_history(payload)
    assert kind == 'pay' and hist[0] == (date(2026, 8, 25), 0.2)
    payload['historical'].append({'date': '2026-07-10', 'dividend': 0.2})       # no payment date
    hist, kind = parse_provider_history(payload)
    assert kind == 'ex' and hist[0][0] == date(2026, 7, 10)
    assert parse_provider_history({}) == ([], 'ex') and parse_provider_history(None) == ([], 'ex')


# ---- tax scaling ---------------------------------------------------------------------------------

def test_net_ratio_from_the_accounts_own_rows():
    rows = [{'amount': 0.85, 'gross_amount': 1.0}, {'amount': 1.70, 'gross_amount': 2.0}, {'amount': 3}]
    assert net_ratio(rows) == pytest.approx(0.85)
    assert net_ratio([{'amount': 1.0}]) is None and net_ratio([]) is None
    assert net_ratio([{'amount': 5.0, 'gross_amount': 1.0}]) == 1.0                 # clamped


# ---- broker metadata -------------------------------------------------------------------------------

def test_broker_per_payment_rate_with_a_matching_hint():
    meta = {'rate': 0.26, 'pay_date': date(2026, 10, 20), 'next_date': None}
    ev, info = broker_forecast(meta, 10, TODAY, 2, ('months', 1), 0.25)
    assert [e['date'] for e in ev] == [date(2026, 10, 20), date(2026, 11, 20)] and ev[0]['per_share'] == 0.26
    assert info['cadence'] == ('months', 1)


def test_broker_annual_rate_is_divided_by_the_frequency_it_matches():
    meta = {'rate': 3.0, 'pay_date': date(2026, 10, 20)}          # 12 x 0.25
    ev, info = broker_forecast(meta, 10, TODAY, 2, None, 0.25)
    assert info['cadence'] == ('months', 1) and ev[0]['per_share'] == pytest.approx(0.25)
    weekly, _ = broker_forecast({'rate': 5.2, 'pay_date': date(2026, 10, 6)}, 10, TODAY, 2, None, 0.1)
    assert len(weekly) >= 8 and weekly[0]['per_share'] == pytest.approx(0.1)


def test_broker_quarterly_with_the_next_date_beyond_the_horizon_gives_no_events():
    meta = {'rate': 0.5, 'pay_date': date(2027, 1, 20)}
    ev, info = broker_forecast(meta, 10, TODAY, 2, ('months', 3), 0.5)
    assert ev == [] and info['status'] == 'ok'


def test_broker_metadata_is_not_used_without_a_cross_check_or_a_date_or_a_rate():
    assert broker_forecast({'rate': 0.3, 'pay_date': date(2026, 10, 20)}, 10, TODAY, 2, None, None) == ([], None)
    assert broker_forecast({'rate': 0.3}, 10, TODAY, 2, None, 0.3) == ([], None)                # no date
    assert broker_forecast({'pay_date': date(2026, 10, 20)}, 10, TODAY, 2, None, 0.3) == ([], None)
    assert broker_forecast({'rate': 0.3, 'pay_date': date(2026, 9, 1)}, 10, TODAY, 2, None, 0.3) == ([], None)
    assert broker_forecast({'rate': 0.99, 'pay_date': date(2026, 10, 20)}, 10, TODAY, 2, ('months', 1), 0.25) == ([], None)
    assert broker_forecast(None, 10, TODAY, 2, None, 0.3) == ([], None)


def test_broker_next_date_is_the_fallback_when_there_is_no_pay_date():
    ev, _ = broker_forecast({'rate': 0.25, 'next_date': date(2026, 10, 12)}, 4, TODAY, 2, None, 0.25)
    assert ev == [{'date': date(2026, 10, 12), 'per_share': 0.25, 'amount': 1.0}]          # single payment


# ---- the page's source priority --------------------------------------------------------------------

def _tab(provider=None, instances=None, monkeypatch=None):
    import ba2_trade_platform.ui.pages.overview as ov
    monkeypatch.setattr(ov, 'get_labels_by_symbol', lambda syms: {})
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    tab._broker_cache = {'instances': instances or {}}
    tab._provider_dividend_histories = (lambda syms: provider or {})
    return tab


def _account_rows(sym='XYZ', aid=1, amount=2.5, gross=None, n=5):
    d = date(TODAY.year, TODAY.month, 15)
    if d >= TODAY:
        d = add_months(d, -1)
    return [{'symbol': sym, 'account_id': aid, 'amount': amount, 'drip_quantity': None,
             **({'gross_amount': gross} if gross else {}),
             'date': datetime(*add_months(d, -k).timetuple()[:3])} for k in range(n)]


def test_history_beats_account_and_scales_to_net(monkeypatch):
    rows = _account_rows(amount=0.85, gross=1.0)                  # observed ratio 0.85
    provider = {'XYZ': (monthly(amt=0.30), 'pay')}
    tab = _tab(provider, monkeypatch=monkeypatch)
    out = tab._compute_dividend_forecast(rows, [], {(1, 'XYZ'): 10.0})
    d = tab._forecast_detail[0]
    assert d['source'] == 'history' and d['basis'] == 'net'
    assert round(out['2026-10']['total'], 2) == pytest.approx(10 * 0.30 * 0.85, abs=0.01)
    assert out['2026-10']['sources'] == {'history': pytest.approx(2.55, abs=0.01)}


def test_without_an_observed_ratio_the_forecast_stays_gross_and_says_so(monkeypatch):
    rows = _account_rows(amount=1.0)                              # no gross_amount anywhere
    tab = _tab({'XYZ': (monthly(amt=0.30), 'pay')}, monkeypatch=monkeypatch)
    out = tab._compute_dividend_forecast(rows, [], {(1, 'XYZ'): 10.0})
    assert tab._forecast_detail[0]['basis'] == 'gross'
    assert round(out['2026-10']['total'], 2) == 3.0
    text, tip = tab._forecast_caption()
    assert 'GROSS' in text and 'XYZ' in tip


def test_fallback_to_account_history_when_the_provider_has_nothing(monkeypatch):
    rows = _account_rows(amount=2.5)
    tab = _tab({}, monkeypatch=monkeypatch)
    out = tab._compute_dividend_forecast(rows, [], {(1, 'XYZ'): 10.0})
    assert tab._forecast_detail[0]['source'] == 'account'
    assert round(out['2026-10']['total'], 2) == 2.5


def test_a_held_symbol_with_only_provider_history_is_forecast_too(monkeypatch):
    tab = _tab({'NEW': (monthly(amt=0.20), 'pay')}, monkeypatch=monkeypatch)
    out = tab._compute_dividend_forecast([], [], {(1, 'NEW'): 5.0})        # bought recently: no account rows
    assert tab._forecast_detail[0]['source'] == 'history' and round(out['2026-10']['total'], 2) == 1.0


def test_divo_style_outlier_is_still_guarded_in_the_account_fallback(monkeypatch):
    d = [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30)]
    qty_at = {d[0]: 100.0, d[1]: 0.5, d[2]: 100.0, d[3]: 100.0}
    rows = [{'symbol': 'DIVO', 'account_id': 2, 'amount': round(0.16 * qty_at[x], 4), 'drip_quantity': None,
             'date': datetime(x.year, x.month, x.day)} for x in d]
    trades = [{'symbol': 'DIVO', 'account_id': 2, 'side': 'SELL', 'qty': 99.5, 'price': 20, 'date': datetime(2026, 7, 30)},
              {'symbol': 'DIVO', 'account_id': 2, 'side': 'BUY', 'qty': 99.5, 'price': 20, 'date': datetime(2026, 8, 1)}]
    tab = _tab({}, monkeypatch=monkeypatch)
    out = tab._compute_dividend_forecast(rows, trades, {(2, 'DIVO'): 100.0})
    assert tab._forecast_detail[0]['source'] == 'account'
    assert round(out['2026-10']['total'], 1) == pytest.approx(16.0, abs=0.2)


class FakeTasty:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get_dividend_metadata(self, symbols):
        self.calls.append(list(symbols))
        return self.data


def test_broker_metadata_wins_and_is_batched_and_cached_for_the_ttl(monkeypatch):
    import ba2_trade_platform.ui.pages.overview as ov
    clock = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(ov, '_clock', lambda: clock.t)
    inst = FakeTasty({'XYZ': {'rate': 0.31, 'pay_date': date(2026, 10, 20), 'next_date': None,
                              'ex_date': None, 'yield': None}})
    rows = _account_rows(amount=3.0)
    tab = _tab({'XYZ': (monthly(amt=0.30), 'pay')}, {1: inst}, monkeypatch)
    out = tab._compute_dividend_forecast(rows, [], {(1, 'XYZ'): 10.0})
    assert tab._forecast_detail[0]['source'] == 'broker'
    assert out['2026-10']['sources'] == {'broker': pytest.approx(3.1, abs=0.01)}
    tab._compute_dividend_forecast(rows, [], {(1, 'XYZ'): 10.0})
    assert len(inst.calls) == 1 and inst.calls[0] == ['XYZ']           # one batched request, cached
    clock.t += 61
    tab._compute_dividend_forecast(rows, [], {(1, 'XYZ'): 10.0})
    assert len(inst.calls) == 2


def test_broker_failure_or_no_support_falls_through_to_history(monkeypatch):
    class Boom:
        def get_dividend_metadata(self, symbols):
            raise RuntimeError('down')
    for inst in (Boom(), SimpleNamespace(), FakeTasty({}), FakeTasty({'XYZ': {'rate': None}})):
        tab = _tab({'XYZ': (monthly(amt=0.30), 'pay')}, {1: inst}, monkeypatch)
        tab._compute_dividend_forecast(_account_rows(), [], {(1, 'XYZ'): 10.0})
        assert tab._forecast_detail[0]['source'] == 'history'


def test_provider_errors_fall_back_with_one_warning(monkeypatch):
    import ba2_trade_platform.ui.pages.overview as ov
    warned = []
    monkeypatch.setattr(ov.logger, 'warning', lambda msg, *a, **k: warned.append(str(msg)))
    import ba2_providers.symbol_info as si
    monkeypatch.setattr(ov, 'get_labels_by_symbol', lambda syms: {})
    import ba2_trade_platform.config as cfg
    monkeypatch.setattr(cfg, 'get_app_setting', lambda k, d=None: 'KEY')
    monkeypatch.setattr(si, 'fetch_dividends', lambda key, sym: (_ for _ in ()).throw(RuntimeError('429')))
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    tab._broker_cache = {'instances': {}}
    out = tab._compute_dividend_forecast(_account_rows('A') + _account_rows('B'), [],
                                         {(1, 'A'): 10.0, (1, 'B'): 10.0})
    assert {d['source'] for d in tab._forecast_detail} == {'account'}
    assert len([m for m in warned if 'provider history unavailable' in m]) == 1


def test_provider_histories_are_cached_per_symbol_for_the_session(monkeypatch):
    import ba2_trade_platform.ui.pages.overview as ov
    import ba2_providers.symbol_info as si
    import ba2_trade_platform.config as cfg
    monkeypatch.setattr(cfg, 'get_app_setting', lambda k, d=None: 'KEY')
    calls = []
    payload = {'historical': [{'date': f'2026-0{m}-10', 'dividend': 0.2, 'paymentDate': f'2026-0{m}-25'}
                              for m in range(1, 10)]}
    monkeypatch.setattr(si, 'fetch_dividends', lambda key, sym: calls.append(sym) or payload)
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    got = tab._provider_dividend_histories(['AAA', 'BBB'])
    again = tab._provider_dividend_histories(['AAA', 'BBB'])
    assert sorted(calls) == ['AAA', 'BBB'] and set(got) == set(again) == {'AAA', 'BBB'}


# ---- TastyTradeAccount.get_dividend_metadata ------------------------------------------------------------------

def _tasty():
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount
    acct = TastyTradeAccount.__new__(TastyTradeAccount)
    acct.id = 2
    acct._session = object()
    acct._check_authentication = lambda: True
    acct._run_async = lambda coro: asyncio.new_event_loop().run_until_complete(coro)
    acct._describe_broker_error = lambda e, what: f'{what}: {e}'
    return acct


def test_tastytrade_dividend_metadata_returns_plain_dicts_from_one_request(monkeypatch):
    import tastytrade.metrics as tm
    from decimal import Decimal
    requests = []

    async def fake(session, symbols):
        requests.append(list(symbols))
        return [SimpleNamespace(symbol='divo', dividend_rate_per_share=Decimal('0.16'),
                                dividend_ex_date=date(2026, 10, 28), dividend_next_date=None,
                                dividend_pay_date=date(2026, 10, 31), dividend_yield=Decimal('0.04')),
                SimpleNamespace(symbol='AAA', dividend_rate_per_share=None, dividend_ex_date=None,
                                dividend_next_date=None, dividend_pay_date=None, dividend_yield=None)]
    monkeypatch.setattr(tm, 'get_market_metrics', fake)
    out = _tasty().get_dividend_metadata(['aaa', 'DIVO', 'DIVO'])
    assert requests == [['AAA', 'DIVO']]                                  # ONE batched request
    assert out['DIVO'] == {'rate': 0.16, 'ex_date': date(2026, 10, 28), 'next_date': None,
                           'pay_date': date(2026, 10, 31), 'yield': 0.04}
    assert out['AAA']['rate'] is None


def test_tastytrade_dividend_metadata_failure_is_an_empty_dict(monkeypatch):
    import tastytrade.metrics as tm

    async def boom(session, symbols):
        raise RuntimeError('401')
    monkeypatch.setattr(tm, 'get_market_metrics', boom)
    assert _tasty().get_dividend_metadata(['DIVO']) == {}
    assert _tasty().get_dividend_metadata([]) == {}
