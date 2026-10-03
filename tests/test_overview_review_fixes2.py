"""Second review round: balance-history escalation, load races, price failures, YTD, pay-date trades."""
import asyncio
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

TODAY = date.today()


# ---- YTD early January (F4) -------------------------------------------------------------

def test_ytd_uses_previous_december_for_the_first_week_of_january():
    from ba2_trade_platform.ui.utils.overview_range import resolve_range_start
    for day in range(1, 8):
        assert resolve_range_start('YTD', date(2027, 1, day)) == date(2026, 12, 1), day
    assert resolve_range_start('YTD', date(2027, 1, 8)) == date(2027, 1, 1)
    assert resolve_range_start('YTD', date(2027, 12, 31)) == date(2027, 1, 1)


# ---- forecast: shares BEFORE the payment date's own trades ---------------------------------

def test_a_sale_on_the_payment_date_is_not_applied_to_that_payment():
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    from ba2_trade_platform.ui.utils.dividend_forecast import add_months
    d = date(TODAY.year, TODAY.month, 15)
    if d >= TODAY:
        d = add_months(d, -1)
    divs = [{'symbol': 'XYZ', 'account_id': 1, 'amount': 50.0, 'drip_quantity': None,
             'date': datetime(*add_months(d, -k).timetuple()[:3])} for k in range(5)]
    sale = {'symbol': 'XYZ', 'account_id': 1, 'side': 'SELL', 'qty': 90, 'price': 20,
            'date': divs[0]['date']}                                   # same day as the last payment
    tab = AccountGrowthTab.__new__(AccountGrowthTab)
    out = tab._compute_dividend_forecast(divs, [sale], {(1, 'XYZ'): 10.0})
    assert out and {round(v['total'], 2) for v in out.values()} == {5.0}


# ---- cache fixtures --------------------------------------------------------------------------

ACCOUNT_OPEN = TODAY - timedelta(days=5 * 365)


class Acct:
    def __init__(self, aid=1, first_trade_days_ago=4 * 365, delay=0.0, slow_first=0.0):
        self.aid, self.calls, self.delay, self.slow_first = aid, [], delay, slow_first
        self.first_trade = TODAY - timedelta(days=first_trade_days_ago)

    def get_balance_history(self, start_date=None, end_date=None):
        self.calls.append(('balance', start_date.date() if start_date else None,
                           end_date is not None))
        n = len([c for c in self.calls if c[0] == 'balance'])
        time.sleep(self.slow_first if n == 1 else self.delay)
        first = (TODAY - timedelta(days=365) if start_date is None
                 else max(start_date.date(), ACCOUNT_OPEN))
        out, d = [], first
        while d < TODAY:
            out.append({'date': datetime.combine(d, datetime.min.time()),
                        'net_liquidating_value': 1000.0, 'cash_balance': 10.0})
            d += timedelta(days=7)
        return out

    def get_dividends(self, **k):
        self.calls.append(('dividends',))
        return [{'symbol': 'AAA', 'amount': 5.0,
                 'date': datetime.combine(self.first_trade + timedelta(days=90), datetime.min.time())}]

    def get_filled_trades(self, **k):
        self.calls.append(('trades',))
        return [{'symbol': 'AAA', 'side': 'BUY', 'qty': 1, 'price': 10.0,
                 'date': datetime.combine(self.first_trade, datetime.min.time())}]

    def get_positions(self):
        self.calls.append(('positions',))
        return [SimpleNamespace(symbol='AAA', qty=1, cost_basis=10.0, avg_entry_price=10.0)]


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-overview-review-fixes2'), request=None)
    yield client
    client.remove_elements(client.elements.values())


@pytest.fixture
def env(monkeypatch, nicegui_client):
    import yfinance
    import ba2_trade_platform.ui.pages.overview as ov
    drawn, yf_calls = [], []
    state = SimpleNamespace(acct=Acct(), yf_fail=False)
    monkeypatch.setattr(ov, 'get_all_instances', lambda m: [SimpleNamespace(id=1, name='A1')])
    monkeypatch.setattr(ov, 'get_account_instance_from_id', lambda i: state.acct)

    def fake_download(*a, **k):
        yf_calls.append(k.get('period'))
        if state.yf_fail:
            raise RuntimeError('yahoo down')
    monkeypatch.setattr(yfinance, 'download', fake_download)
    monkeypatch.setattr(ov, '_extract_yf_close_prices', lambda h, s: {'2026-09-01': 1.0})
    for name in ('_render_monthly_realized_income_chart', '_render_monthly_profit_by_label_chart',
                 '_render_growth_by_label_charts', '_render_growth_by_position_in_label_charts',
                 '_render_per_position_section', '_render_dividend_history_table',
                 '_render_scope_controls'):
        monkeypatch.setattr(ov.AccountGrowthTab, name, lambda *a, **k: None)

    def rec(self, bal, divs, trades):
        bd = [b['date'].date() for b in bal]
        drawn.append({'range': self._range, 'n_bal': len(bal),
                      'balance_first': min(bd) if bd else None,
                      'capped': ov.history_capped(self._raw_range_start, self._balance_first,
                                                  self._activity_first)})
    monkeypatch.setattr(ov.AccountGrowthTab, '_render_total_growth_chart', rec)

    def new_tab(rk):
        tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
        tab._scope, tab._account_ids, tab._single_account = None, [], None
        tab._forecast, tab._gen, tab._hidden_labels = {}, 0, 0
        tab._init_data_cache()
        tab._set_range(rk)
        return tab
    return SimpleNamespace(ov=ov, drawn=drawn, yf=yf_calls, state=state, new_tab=new_tab,
                           client=nicegui_client)


def _balance_calls(acct):
    return [c for c in acct.calls if c[0] == 'balance']


# ---- F1: YTD -> 3y -> Max really fetches the longer history -----------------------------------

def test_ytd_then_3y_then_max_fetches_longer_balance_history_once_each(env):
    from nicegui import ui

    async def go():
        tab = env.new_tab('YTD')
        with env.client.content:
            for rk in ('YTD', '3y', 'Max', 'Max', 'Max', '3y', '1y'):
                tab._set_range(rk)
                await tab._load_growth_data(ui.label('x'), ui.column(), 1)
        return tab
    tab = asyncio.run(go())
    calls = _balance_calls(env.state.acct)
    assert len(calls) == 3 and calls[0][1] is None
    assert calls[1][1] is not None and calls[2][1] is not None and calls[2][1] < calls[1][1]
    assert all(c[2] for c in calls[1:])                 # explicit end_date with start_date
    shown = {}
    for d in env.drawn:
        shown.setdefault(d['range'], d)      # first draw of each range
    assert shown['YTD']['n_bal'] < shown['3y']['n_bal'] < shown['Max']['n_bal']
    assert shown['Max']['balance_first'] < shown['3y']['balance_first']
    assert not shown['Max']['capped']                   # the title must not claim a cap that is not there
    assert env.yf == ['1y', '3y', 'max']


def test_max_repeated_when_the_broker_returns_less_asks_only_once(env):
    from nicegui import ui

    async def go():
        tab = env.new_tab('Max')
        with env.client.content:
            for _ in range(3):
                await tab._load_growth_data(ui.label('x'), ui.column(), 1)
    asyncio.run(go())
    assert len(_balance_calls(env.state.acct)) == 1


# ---- F2: a stale (older) load must not poison the cache or the page state -----------------------

def test_slow_default_load_finishing_after_a_max_click_does_not_overwrite_history(env):
    from nicegui import ui
    env.state.acct = Acct(slow_first=1.0)

    async def go():
        tab = env.new_tab('1y')
        with env.client.content:
            t1 = asyncio.create_task(tab._load_growth_data(ui.label('x'), ui.column(), 1))
            await asyncio.sleep(0.2)
            tab._set_range('Max')
            t2 = asyncio.create_task(tab._load_growth_data(ui.label('y'), ui.column(), 1))
            await asyncio.gather(t1, t2)
            first_pass = list(env.drawn)
            env.drawn.clear()
            await tab._load_growth_data(ui.label('z'), ui.column(), 1)
        return tab, first_pass
    tab, first_pass = asyncio.run(go())
    assert [d['range'] for d in first_pass] == ['Max']          # the stale 1y load never drew
    assert first_pass[0]['n_bal'] > 100
    assert env.drawn[0]['n_bal'] == first_pass[0]['n_bal']       # next Max draw still has it all
    assert not env.drawn[0]['capped']
    assert len(env.state.acct.calls and _balance_calls(env.state.acct)) == 2


def test_a_click_during_the_first_load_does_not_refetch_range_independent_data(env):
    from nicegui import ui
    env.state.acct = Acct(slow_first=0.5)

    async def go():
        tab = env.new_tab('1y')
        with env.client.content:
            t1 = asyncio.create_task(tab._load_growth_data(ui.label('x'), ui.column(), 1))
            await asyncio.sleep(0.1)
            tab._set_range('3y')
            t2 = asyncio.create_task(tab._load_growth_data(ui.label('y'), ui.column(), 1))
            await asyncio.gather(t1, t2)
    asyncio.run(go())
    calls = env.state.acct.calls
    for name in ('dividends', 'trades', 'positions'):
        assert len([c for c in calls if c[0] == name]) == 1, calls


# ---- F3b: a failed price download is not cached -------------------------------------------------

def test_failed_price_download_is_retried_on_the_next_load(env):
    from nicegui import ui
    env.state.yf_fail = True

    async def go():
        tab = env.new_tab('YTD')
        with env.client.content:
            await tab._load_growth_data(ui.label('x'), ui.column(), 1)
            env.state.yf_fail = False
            await tab._load_growth_data(ui.label('y'), ui.column(), 1)
            await tab._load_growth_data(ui.label('z'), ui.column(), 1)
        return tab
    tab = asyncio.run(go())
    assert env.yf == ['1y', '1y']                               # failed once, retried once, then cached
    assert tab._broker_cache['prices']['data']
