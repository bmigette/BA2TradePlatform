"""Broker-data cache TTL: never stale beyond a short window; late-booked rows show up.

Call-counting fake account + a controllable clock (``overview._clock``).
"""
import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.test_overview_review_fixes2 import nicegui_client  # noqa: F401  (fixture)

TODAY = date.today()


def _dt(d):
    return datetime.combine(d, datetime.min.time())


class LiveAcct:
    """Mutable broker: tests append dividends between fetches."""

    def __init__(self):
        self.calls = []
        self.divs = []
        self.balance_days = 400

    def n(self, name):
        return len([c for c in self.calls if c == name])

    def get_balance_history(self, start_date=None, end_date=None):
        self.calls.append('balance')
        return [{'date': _dt(TODAY - timedelta(days=d)), 'net_liquidating_value': 1000.0,
                 'cash_balance': 10.0} for d in range(30, 0, -1)]

    def get_dividends(self, **k):
        self.calls.append('dividends')
        return [dict(d) for d in self.divs]

    def get_filled_trades(self, **k):
        self.calls.append('trades')
        return [{'symbol': s, 'side': 'BUY', 'qty': 1, 'price': 10.0, 'date': _dt(TODAY - timedelta(days=300))}
                for s in ('AAA', 'BBB')]

    def get_positions(self):
        self.calls.append('positions')
        return [SimpleNamespace(symbol=s, qty=1, cost_basis=10.0, avg_entry_price=10.0)
                for s in ('AAA', 'BBB')]


def _div(sym, days_ago, amount=5.0):
    return {'symbol': sym, 'amount': amount, 'date': _dt(TODAY - timedelta(days=days_ago)),
            'drip_quantity': None}


@pytest.fixture
def env(monkeypatch, nicegui_client):  # noqa: F811
    import yfinance
    import ba2_trade_platform.ui.pages.overview as ov
    acct = LiveAcct()
    clock = SimpleNamespace(t=1000.0)
    seen = []
    monkeypatch.setattr(ov, '_clock', lambda: clock.t)
    monkeypatch.setattr(ov, 'get_all_instances', lambda m: [SimpleNamespace(id=1, name='A1')])
    monkeypatch.setattr(ov, 'get_account_instance_from_id', lambda i: acct)
    monkeypatch.setattr(ov, 'get_labels_by_symbol', lambda syms: {})
    monkeypatch.setattr(yfinance, 'download', lambda *a, **k: None)
    monkeypatch.setattr(ov, '_extract_yf_close_prices', lambda h, s: {'2026-09-01': 1.0})
    for name in ('_render_monthly_profit_by_label_chart', '_render_total_growth_chart',
                 '_render_growth_by_label_charts', '_render_growth_by_position_in_label_charts',
                 '_render_per_position_section', '_render_dividend_history_table',
                 '_render_scope_controls'):
        monkeypatch.setattr(ov.AccountGrowthTab, name, lambda *a, **k: None)

    def monthly(self, months, income, glob=None):
        seen.append({'income': {m: v['div'] for m, v in income.items()},
                     'forecast': dict(self._forecast)})
    monkeypatch.setattr(ov.AccountGrowthTab, '_render_monthly_realized_income_chart', monthly)

    def new_tab():
        tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
        tab._scope, tab._account_ids, tab._single_account = None, [], None
        tab._forecast, tab._gen, tab._hidden_labels = {}, 0, 0
        tab._init_data_cache()
        tab._set_range('YTD')
        return tab

    async def load(tab):
        from nicegui import ui
        with nicegui_client.content:
            await tab._load_growth_data(ui.label('x'), ui.column(), 1)
    return SimpleNamespace(ov=ov, acct=acct, clock=clock, seen=seen, new_tab=new_tab, load=load)


def test_constants_are_named_and_sane():
    import ba2_trade_platform.ui.pages.overview as ov
    assert ov.BROKER_DATA_TTL_SECONDS == 60
    assert ov.PRICE_TTL_SECONDS == 15 * 60
    assert ov.AUTO_REFRESH_SECONDS == 5 * 60


def test_clicks_within_the_ttl_cost_no_broker_calls(env):
    async def go():
        tab = env.new_tab()
        await env.load(tab)
        for dt in (5, 20, 59):
            env.clock.t += dt if dt == 5 else 0
            tab._set_range('6m')
            await env.load(tab)
            tab._set_range('YTD')
            await env.load(tab)
    asyncio.run(go())
    assert [env.acct.n(n) for n in ('dividends', 'trades', 'positions', 'balance')] == [1, 1, 1, 1]


def test_after_the_ttl_exactly_one_full_refetch_of_every_dataset(env):
    async def go():
        tab = env.new_tab()
        await env.load(tab)
        env.clock.t += 61
        await env.load(tab)
        tab._set_range('6m')                      # same moment: still fresh again
        await env.load(tab)
    asyncio.run(go())
    assert [env.acct.n(n) for n in ('dividends', 'trades', 'positions', 'balance')] == [2, 2, 2, 2]


def test_a_dividend_booked_two_days_back_appears_after_the_ttl_in_chart_and_forecast(env):
    # BBB has ONE payment on record: no cadence, no forecast.
    env.acct.divs = [_div('BBB', 33)]

    async def go():
        tab = env.new_tab()
        await env.load(tab)
        first = dict(env.seen[-1])
        env.acct.divs.append(_div('BBB', 2, 7.0))        # late-booked, dated 2 days ago
        await env.load(tab)                               # inside the TTL: still the old picture
        within = dict(env.seen[-1])
        env.clock.t += 61
        await env.load(tab)
        return first, within, dict(env.seen[-1])
    first, within, after = asyncio.run(go())
    month = (TODAY - timedelta(days=2)).strftime('%Y-%m')
    assert first['forecast'] == {} and within == first
    assert after['income'].get(month, 0) >= 7.0
    assert after['forecast'], 'the new second payment must create a forecast'


def test_a_dividend_added_today_twice_shows_after_each_ttl_expiry(env):
    async def go():
        tab = env.new_tab()
        out = []
        await env.load(tab)
        out.append(sum(env.seen[-1]['income'].values()))
        for amount in (3.0, 4.0):
            env.acct.divs.append(_div('AAA', 0, amount))
            env.clock.t += 61
            await env.load(tab)
            out.append(sum(env.seen[-1]['income'].values()))
        return out
    assert asyncio.run(go()) == [0, 3.0, 7.0]


def test_fresh_page_and_account_change_start_with_an_empty_cache(env):
    async def go():
        tab = env.new_tab()
        await env.load(tab)
        assert tab._broker_cache.get('dividends') is not None
        other = env.new_tab()                              # a fresh page load
        assert other._broker_cache == {}
        tab._account_ids = [99]                            # account selection changed
        tab._broker_cache['key'] = (99,)
        await env.load(tab)                                # key (1,) != (99,) -> cleared and refetched
        return tab
    tab = asyncio.run(go())
    assert env.acct.n('dividends') == 2 and tab._broker_cache['key'] == (1,)


def test_updated_label_text_and_fingerprint():
    import ba2_trade_platform.ui.pages.overview as ov
    a = ov.rows_fingerprint([{'amount': 1, 'date': '2026-10-01'}], [], [])
    b = ov.rows_fingerprint([{'amount': 1, 'date': '2026-10-01'}, {'amount': 2, 'date': '2026-10-01'}], [], [])
    c = ov.rows_fingerprint([{'amount': 1, 'date': '2026-10-01'}], [], [])
    assert a == c and a != b


def test_updated_stamp_follows_fetches_not_cached_draws(env):
    async def go():
        tab = env.new_tab()
        await env.load(tab)
        t1 = tab._broker_cache['updated_wall']
        await env.load(tab)
        same = tab._broker_cache['updated_wall'] is t1
        env.clock.t += 61
        await env.load(tab)
        return same, tab._broker_cache['updated_wall'] is not t1
    assert asyncio.run(go()) == (True, True)


def test_price_cache_has_its_own_longer_ttl(env, monkeypatch):
    import yfinance
    calls = []
    monkeypatch.setattr(yfinance, 'download', lambda *a, **k: calls.append(1))

    async def go():
        tab = env.new_tab()
        await env.load(tab)
        env.clock.t += 120                                  # broker TTL expired, prices still fresh
        await env.load(tab)
        env.clock.t += 900                                  # now the price TTL is over too
        await env.load(tab)
    asyncio.run(go())
    assert len(calls) == 2
