"""Forecast on the REAL declared series (FMP stock_dividend, live TastyTrade holdings, 2026-10-03):
CAS stepping up, SLVO/OVL/HSHP long histories, MAGS annual, MAIN with quarterly supplements."""
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ba2_trade_platform.ui.utils.dividend_forecast import (
    drop_specials, forecast_dividends_ex, parse_provider_history,
)

TODAY = date(2026, 10, 3)
SERIES = json.loads((Path(__file__).parent / 'data' / 'live_dividend_series.json').read_text(encoding='utf-8'))
QTY = {'CAS': 16.0, 'SLVO': 4.0, 'OVL': 1.71, 'HSHP': 3.34, 'MAGS': 0.65, 'MAIN': 1.70}


def run(sym):
    hist, _kind = parse_provider_history({'historical': SERIES[sym]})
    return forecast_dividends_ex(hist, QTY[sym], TODAY, months=2, specials=True, include_declared=True)


def monthly_totals(events):
    out = {}
    for e in events:
        out[e['date'].strftime('%Y-%m')] = out.get(e['date'].strftime('%Y-%m'), 0.0) + e['amount']
    return out


def test_cas_stepped_up_to_1_00_is_forecast_at_the_latest_amount():
    ev, info = run('CAS')
    assert info['status'] == 'ok'
    assert info['per_share'] == pytest.approx(1.0)                      # not the median 0.5 of the last 3
    tot = monthly_totals(ev)
    assert tot['2026-10'] == pytest.approx(16.0, abs=0.01) and tot['2026-11'] == pytest.approx(16.0, abs=0.01)


@pytest.mark.parametrize('sym,low,high', [('SLVO', 1.0, 2.5), ('OVL', 0.45, 0.52), ('HSHP', 0.2, 0.3)])
def test_long_histories_with_a_raised_payout_are_not_silently_zero(sym, low, high):
    ev, info = run(sym)
    assert info['status'] == 'ok', info
    assert low <= info['per_share'] <= high
    assert ev and monthly_totals(ev).get('2026-10', 0) > 0


def test_mags_annual_payer_has_no_phantom_forecast_on_today():
    ev, info = run('MAGS')
    assert info['status'] == 'ok' and info['cadence'] == ('months', 12)
    assert ev == []                                                      # next payment 2026-12-31: beyond


def test_main_quarterly_supplements_are_still_excluded():
    ev, info = run('MAIN')
    assert info['status'] == 'ok' and info['cadence'] == ('months', 1)
    assert info['per_share'] == pytest.approx(0.265, abs=0.005)
    assert [e['date'] for e in ev][:2] == [date(2026, 10, 15), date(2026, 11, 13)]    # declared, regular


def test_a_spike_is_only_a_special_against_payments_on_BOTH_sides():
    d = lambda m: date(2026, m, 15)  # noqa: E731
    stepped = {d(1): 0.1, d(2): 0.1, d(3): 0.1, d(4): 0.5, d(5): 0.5, d(6): 1.0}
    assert drop_specials(stepped) == stepped                              # a raise is not a special
    spike = {d(1): 0.2, d(2): 0.2, d(3): 0.2, d(4): 1.5, d(5): 0.2, d(6): 0.2}
    assert d(4) not in drop_specials(spike) and len(drop_specials(spike)) == 5
    latest_spike = {d(1): 0.2, d(2): 0.2, d(3): 0.2, d(4): 1.5}           # nothing after: kept
    assert d(4) in drop_specials(latest_spike)


def test_staleness_uses_the_unfiltered_last_payment_date():
    # every late payment looks like a "special" to a filter; the series is still current
    hist = [(date(2026, 1, 15), 0.1), (date(2026, 2, 15), 0.1), (date(2026, 3, 15), 0.1),
            (date(2026, 4, 15), 0.1), (date(2026, 5, 15), 0.1), (date(2026, 9, 15), 0.1)]
    ev, info = forecast_dividends_ex(hist, 10, TODAY, specials=True)
    assert info['status'] != 'stale'


def test_stepping_up_payers_use_the_latest_amount():
    rising = [(date(2026, 7, 15), 0.5), (date(2026, 8, 15), 0.5), (date(2026, 9, 15), 1.0)]
    assert forecast_dividends_ex(rising, 1, TODAY)[1]['per_share'] == pytest.approx(1.0)
    agree = [(date(2026, 7, 15), 0.30), (date(2026, 8, 15), 0.10), (date(2026, 9, 15), 0.105)]
    assert forecast_dividends_ex(agree, 1, TODAY)[1]['per_share'] == pytest.approx(0.105)      # last two within 10%
    jumpy = [(date(2026, 7, 15), 0.11), (date(2026, 8, 15), 0.30), (date(2026, 9, 15), 0.10)]
    assert forecast_dividends_ex(jumpy, 1, TODAY)[1]['per_share'] == pytest.approx(0.11)       # median


# ---- page-level: broker hint only from an 'ok' history, per-symbol tax ratio, gross/net, per-label detail ----------------

def _tab(monkeypatch, provider, instances=None, positions=None, labels=None):
    import ba2_trade_platform.ui.pages.overview as ov
    monkeypatch.setattr(ov, 'get_labels_by_symbol', lambda syms: labels or {})
    tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
    tab._broker_cache = {'instances': instances or {}, 'positions': positions or []}
    tab._provider_dividend_histories = lambda syms: provider
    return tab


class _Meta:
    def __init__(self, data):
        self.data = data

    def get_dividend_metadata(self, symbols):
        return self.data


def test_a_stale_history_does_not_feed_the_broker_cross_check(monkeypatch):
    # FMP history ended long ago at 0.10/month; the broker says 1.00 per payment, pay date soon.
    # With the stale 0.10 as a hint 1.00 would read as an ANNUAL rate (/12). It must not be used.
    stale = [(date(2025, m, 15), 0.10) for m in range(1, 7)]
    tab = _tab(monkeypatch, {'ZZZ': (stale, 'pay')},
               {1: _Meta({'ZZZ': {'rate': 1.0, 'pay_date': date(2026, 10, 20), 'next_date': None}})})
    out = tab._compute_dividend_forecast([], [], {(1, 'ZZZ'): 10.0})
    assert out == {} or all(s != 'broker' for slot in out.values() for s in slot['sources'])
    assert tab._forecast_detail[0]['source'] != 'broker'


def test_tax_ratio_is_per_symbol_when_the_rows_allow_it(monkeypatch):
    def rows(sym, net, gross):
        return [{'symbol': sym, 'account_id': 1, 'amount': net, 'gross_amount': gross, 'drip_quantity': None,
                 'date': datetime(2026, m, 15)} for m in range(5, 10)]
    divs = rows('TAXED', 0.85, 1.0) + rows('ETN', 1.0, 1.0)        # ETN: no withholding
    prov = {'TAXED': ([(date(2026, m, 15), 1.0) for m in range(4, 10)], 'pay'),
            'ETN': ([(date(2026, m, 15), 1.0) for m in range(4, 10)], 'pay'),
            'NEW': ([(date(2026, m, 15), 1.0) for m in range(4, 10)], 'pay')}     # no rows: account ratio
    tab = _tab(monkeypatch, prov)
    out = tab._compute_dividend_forecast(divs, [], {(1, 'TAXED'): 10.0, (1, 'ETN'): 10.0, (1, 'NEW'): 10.0})
    det = {d['symbol']: d for d in tab._forecast_detail}
    assert det['TAXED']['events'][0][1] == pytest.approx(8.5) and det['ETN']['events'][0][1] == pytest.approx(10.0)
    assert det['NEW']['events'][0][1] == pytest.approx(9.25, abs=0.01)         # account ratio 0.925
    slot = out['2026-10']
    assert slot['total'] == pytest.approx(8.5 + 10.0 + det['NEW']['events'][0][1], abs=0.01)
    assert slot['gross_total'] == pytest.approx(30.0, abs=0.01)


def test_caption_shows_net_and_gross_monthly_totals_and_label_yield(monkeypatch):
    pos = [SimpleNamespace(symbol='AAA', qty=10.0, market_value=1000.0),
           SimpleNamespace(symbol='BBB', qty=5.0, market_value=500.0)]
    divs = [{'symbol': s, 'account_id': 1, 'amount': 0.85, 'gross_amount': 1.0, 'drip_quantity': None,
             'date': datetime(2026, m, 15)} for s in ('AAA', 'BBB') for m in range(5, 10)]
    prov = {s: ([(date(2026, m, 15), 1.0) for m in range(4, 10)], 'pay') for s in ('AAA', 'BBB')}
    tab = _tab(monkeypatch, prov, positions=pos, labels={'AAA': ['L1', 'L2'], 'BBB': ['L2']})
    tab._compute_dividend_forecast(divs, [], {(1, 'AAA'): 10.0, (1, 'BBB'): 5.0})
    text, tip = tab._forecast_caption()
    assert 'net' in text and 'gross' in text.lower()
    assert 'L1' in tip and 'gross' in tip and '% of value' in tip
    lab = tab._forecast_label_detail
    assert lab['L1']['gross'] == pytest.approx(10.0) and lab['L1']['net'] == pytest.approx(8.5)
    assert lab['L1']['value'] == pytest.approx(1000.0) and lab['L1']['yield_pct'] == pytest.approx(1.0)
    assert lab['L2']['gross'] == pytest.approx(15.0) and lab['L2']['value'] == pytest.approx(1500.0)
