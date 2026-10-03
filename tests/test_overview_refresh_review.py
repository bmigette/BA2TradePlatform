"""Review round 3: refresh lock, auto-refresh, failed fetches, expiry routing, fingerprint.

Drives the REAL ``render()`` closures (refresh button, timer callback) with a controllable
clock and a counting broker stub.
"""
import asyncio
import threading
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

TODAY = date.today()


def _dt(d):
    return datetime.combine(d, datetime.min.time())


class Broker:
    def __init__(self, delay=0.0, aid=1):
        self.aid, self.delay = aid, delay
        self.calls, self.inflight, self.max_inflight = [], 0, 0
        self.lock = threading.Lock()
        self.divs = [{'symbol': 'AAA', 'amount': 5.0, 'date': _dt(TODAY - timedelta(days=d)),
                      'drip_quantity': None} for d in (95, 185, 275)]
        self.trades = [{'symbol': 'AAA', 'side': 'BUY', 'qty': 10, 'price': 10.0,
                        'date': _dt(TODAY - timedelta(days=400))}]
        self.positions = [SimpleNamespace(symbol='AAA', qty=10, avg_entry_price=10.0)]
        self.nlv = 1000.0
        self.fail = set()

    def _enter(self, name):
        with self.lock:
            self.calls.append(name)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        time.sleep(self.delay)
        with self.lock:
            self.inflight -= 1

    def n(self, name):
        return self.calls.count(name)

    def get_balance_history(self, start_date=None, end_date=None):
        self._enter('balance')
        return [{'date': _dt(TODAY - timedelta(days=d)), 'net_liquidating_value': self.nlv,
                 'cash_balance': 10.0} for d in range(30, 0, -1)]

    def get_dividends(self, **k):
        self._enter('dividends')
        return [] if 'dividends' in self.fail else [dict(d) for d in self.divs]

    def get_filled_trades(self, **k):
        self._enter('trades')
        return [] if 'trades' in self.fail else [dict(t) for t in self.trades]

    def get_positions(self):
        self._enter('positions')
        return None if 'positions' in self.fail else list(self.positions)


@pytest.fixture
def env(monkeypatch):
    import yfinance
    from nicegui import ui
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    import ba2_trade_platform.ui.pages.overview as ov
    clock = SimpleNamespace(t=1000.0)
    drawn, notes, captured, yf_calls = [], [], {}, []
    state = SimpleNamespace(brokers=[Broker()], selected=1)
    monkeypatch.setattr(ov, '_clock', lambda: clock.t)
    monkeypatch.setattr(ov, 'get_all_instances',
                        lambda m: [SimpleNamespace(id=b.aid, name=f'A{b.aid}') for b in state.brokers])
    monkeypatch.setattr(ov, 'get_account_instance_from_id',
                        lambda i: next(b for b in state.brokers if b.aid == i))
    monkeypatch.setattr(ov, 'get_selected_account_id', lambda: state.selected)
    monkeypatch.setattr(ov, 'read_range', lambda: 'YTD')
    monkeypatch.setattr(ov, 'write_range', lambda v: True)
    monkeypatch.setattr(ov, 'get_labels_by_symbol', lambda syms: {})
    monkeypatch.setattr(yfinance, 'download', lambda *a, **k: yf_calls.append(k.get('period')))
    monkeypatch.setattr(ov, '_extract_yf_close_prices', lambda h, s: {'2026-09-01': 1.0})
    from nicegui.slot import Slot

    def notify(msg, *a, **k):
        # production: a create_task'd load has an EMPTY slot stack, so ui.notify raises
        notes.append({'msg': msg, 'ctx_ok': bool(Slot.get_stack()), **k})
        if not Slot.get_stack():
            raise RuntimeError('slot stack empty (what production raises)')
    monkeypatch.setattr(ov.ui, 'notify', notify)
    originals = {n: getattr(ov.AccountGrowthTab, n) for n in (
        '_render_per_position_section', '_render_growth_by_label_charts',
        '_render_position_growth_chart_from_data')}

    async def hidden(code, *a, **k):
        return state.__dict__.get('hidden', False)
    monkeypatch.setattr(ov.ui, 'run_javascript', hidden)
    for name in ('_render_monthly_profit_by_label_chart', '_render_growth_by_label_charts',
                 '_render_growth_by_position_in_label_charts', '_render_per_position_section',
                 '_render_dividend_history_table'):
        monkeypatch.setattr(ov.AccountGrowthTab, name, lambda *a, **k: None)
    monkeypatch.setattr(ov.AccountGrowthTab, '_compute_scope_inputs',
                        lambda self, target, t, d, p: {a.id: {'name': a.name, 'traded': ['Unlabeled'],
                                                              'managed': [], 'stored': None,
                                                              'follow': False} for a, _ in target})
    monkeypatch.setattr(ov.AccountGrowthTab, '_render_scope_controls',
                        lambda self, holder, st, on_change: captured.__setitem__('on_scope_change', on_change))

    def monthly(self, months, income, glob=None):
        drawn.append({'kind': 'monthly', 'income': {m: v['div'] for m, v in income.items()}})
    monkeypatch.setattr(ov.AccountGrowthTab, '_render_monthly_realized_income_chart', monthly)

    def total(self, bal, divs, trades):
        drawn.append({'kind': 'total', 'nlv': bal[-1]['net_liquidating_value'] if bal else None,
                      'n_div': len(divs), 'n_trades': len(trades)})
    monkeypatch.setattr(ov.AccountGrowthTab, '_render_total_growth_chart', total)

    async def settle():
        for _ in range(200):
            await asyncio.sleep(0.05)
            if not any(t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                       and 'load_growth' in repr(t.get_coro())):
                return

    def build():
        client = Client(nicegui_page('/t-refresh-review'), request=None)
        with client.content:
            tab = ov.AccountGrowthTab()
        timers = [e for e in client.elements.values() if type(e).__name__ == 'Timer']
        buttons = [e for e in client.elements.values() if type(e).__name__ == 'Button']
        handlers = {}
        for b in buttons:
            for lst in b._event_listeners.values():
                handlers[b.props.get('icon') or b.text] = lst.handler
        return SimpleNamespace(tab=tab, client=client, timer=timers[-1],
                               refresh=handlers['refresh'], new_data=handlers['sync'],
                               chip=next(b for b in buttons if b.props.get('icon') == 'sync'))

    def last(kind):
        return [d for d in drawn if d['kind'] == kind][-1]
    def restore(name):
        monkeypatch.setattr(ov.AccountGrowthTab, name, originals[name])
    monkeypatch.setattr(ov.AccountGrowthTab, '_render_position_growth_chart_from_data',
                        lambda self, symbol, *a, **k: drawn.append({'kind': 'symbol_chart', 'symbol': symbol}) or {})
    return SimpleNamespace(ov=ov, clock=clock, drawn=drawn, notes=notes, captured=captured,
                           yf=yf_calls, state=state, settle=settle, build=build, last=last, ui=ui,
                           restore=restore)


# ---- 1: one lock, no concurrent fetches ---------------------------------------------------------

def test_repeated_refresh_clicks_never_load_concurrently(env):
    env.state.brokers = [Broker(delay=0.15)]

    async def go():
        page = env.build()
        await env.settle()
        lock = page.tab._load_lock
        broker = env.state.brokers[0]
        broker.max_inflight = 0
        with page.client.content:
            for _ in range(3):
                page.refresh(None)
                await asyncio.sleep(0.03)
        await env.settle()
        return page, lock, broker
    page, lock, broker = asyncio.run(go())
    assert page.tab._load_lock is lock                    # created once, never replaced
    assert broker.max_inflight == 1


def test_timer_does_not_fire_immediately(env):
    async def go():
        page = env.build()
        return page.timer._immediate
    assert asyncio.run(go()) is False


# ---- 2: auto refresh keeps balance and TTL consistent; chip instead of a redraw -------------------

def test_auto_refresh_with_a_moved_balance_updates_silently_without_a_chip(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.calls.clear()
        broker.nlv = 2000.0                         # only intraday equity moved
        env.clock.t += 300
        n_total = len([d for d in env.drawn if d['kind'] == 'total'])
        await page.timer.callback()
        redrawn = len([d for d in env.drawn if d['kind'] == 'total']) != n_total
        chip_shown = page.chip.visible
        calls_auto = list(broker.calls)
        broker.calls.clear()
        env.clock.t += 30
        page.tab._set_range('6m')
        from nicegui import ui
        with page.client.content:
            await page.tab._load_growth_data(ui.label('x'), ui.column(), 1)
        return redrawn, chip_shown, calls_auto, list(broker.calls)
    redrawn, chip_shown, calls_auto, calls_click = asyncio.run(go())
    assert not redrawn and not chip_shown           # balance-only: no redraw, no chip
    assert 'balance' in calls_auto
    assert calls_click == []                        # the click 30 s later is fresh...
    assert env.last('total')['nlv'] == 2000.0       # ...and shows the new equity


def test_auto_refresh_with_changed_rows_offers_the_chip_and_the_tap_draws_from_cache(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.divs.append({'symbol': 'AAA', 'amount': 7.0, 'date': _dt(TODAY - timedelta(days=2)),
                            'drip_quantity': None})
        broker.calls.clear()
        env.clock.t += 300
        n0 = len([d for d in env.drawn if d['kind'] == 'total'])
        await page.timer.callback()
        shown = page.chip.visible
        redrawn = len([d for d in env.drawn if d['kind'] == 'total']) != n0
        broker.calls.clear()
        with page.client.content:
            page.new_data(None)
        await env.settle()
        return page, shown, redrawn, list(broker.calls)
    page, shown, redrawn, calls = asyncio.run(go())
    assert shown and not redrawn
    assert calls == [] and env.last('total')['n_div'] == 4 and not page.chip.visible


def test_auto_refresh_with_nothing_changed_is_silent_and_resets_the_ttl_consistently(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.calls.clear()
        env.clock.t += 300
        await page.timer.callback()
        fresh = page.tab._broker_cache['fetched_at'] == env.clock.t
        env.clock.t += 30
        return page, fresh, list(broker.calls)
    page, fresh, calls = asyncio.run(go())
    assert not page.chip.visible
    assert fresh and 'balance' in calls and calls.count('dividends') == 1


def test_auto_refresh_skips_a_hidden_tab_and_a_running_load(env):
    env.state.brokers = [Broker(delay=0.2)]

    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.calls.clear()
        env.state.hidden = True
        await page.timer.callback()
        hidden_calls = list(broker.calls)
        env.state.hidden = False
        env.clock.t += 400
        with page.client.content:
            page.refresh(None)
            await asyncio.sleep(0.05)
            await page.timer.callback()               # lock is held by the refresh load
        await env.settle()
        return hidden_calls, broker
    hidden_calls, broker = asyncio.run(go())
    assert hidden_calls == []
    assert broker.n('dividends') == 1                  # only the refresh load fetched; the tick was skipped
    assert broker.max_inflight == 1


# ---- 3: never replace good data with an error-shaped result -------------------------------------------

def test_failed_broker_fetch_keeps_the_good_rows_and_warns_once(env):
    async def go():
        page = env.build()
        await env.settle()
        good = dict(env.last('monthly')['income'])
        broker = env.state.brokers[0]
        broker.fail = {'dividends', 'trades', 'positions'}
        env.clock.t += 300
        await page.timer.callback()
        env.clock.t += 300
        await page.timer.callback()
        # a user click after the TTL goes through the same rule
        page.tab._set_range('6m')
        from nicegui import ui
        with page.client.content:
            await page.tab._load_growth_data(ui.label('x'), ui.column(), 1)
        return page, good
    page, good = asyncio.run(go())
    assert good and env.last('monthly')['income'] == good
    assert 'refresh failed' in page.tab._updated_label.text
    assert env.last('total')['n_trades'] == 1 and env.last('total')['n_div'] == 3
    assert len([n for n in env.notes if n.get('type') == 'warning']) >= 1
    assert page.tab._broker_cache['positions']


def test_merge_refresh_is_judged_per_account():
    from ba2_trade_platform.ui.pages.overview import merge_refresh
    prev = {1: {'dividends': [1, 2, 3], 'trades': [4], 'positions': ['p1'], 'balance': [9]},
            2: {'dividends': [5, 6, 7], 'trades': [8], 'positions': ['p2'], 'balance': [9]}}
    new = {1: {'dividends': [1, 2, 3, 10], 'trades': [4], 'positions': ['p1'], 'balance': [9]},
           2: {'dividends': [], 'trades': [], 'positions': [], 'balance': [9]}}
    merged, bad = merge_refresh(prev, new, {(2, 'positions')})
    assert bad == {(2, 'dividends'), (2, 'trades'), (2, 'positions')}
    assert merged[1]['dividends'] == [1, 2, 3, 10]                  # account 1 updates
    assert merged[2]['dividends'] == [5, 6, 7] and merged[2]['trades'] == [8]
    assert merged[2]['positions'] == ['p2']                          # failed -> kept
    # first fetch: nothing to fall back on; an empty history is simply empty
    merged, bad = merge_refresh(None, new, set())
    assert bad == set() and merged[2]['dividends'] == []
    # a flat account (positions really []) is not a failure
    merged, bad = merge_refresh(prev, {1: dict(prev[1], positions=[]), 2: prev[2]}, set())
    assert bad == set() and merged[1]['positions'] == []


# ---- 4: scope / dropdown redraws after the TTL refetch ---------------------------------------------------

def test_scope_change_after_the_ttl_redraws_at_once_and_refreshes_in_the_background(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.divs.append({'symbol': 'AAA', 'amount': 7.0, 'date': _dt(TODAY - timedelta(days=2)),
                            'drip_quantity': None})
        broker.calls.clear()
        env.captured['on_scope_change']()                 # inside the TTL: plain redraw
        within = list(broker.calls)
        env.clock.t += 600
        n0 = len([d for d in env.drawn if d['kind'] == 'total'])
        env.captured['on_scope_change']()                 # expired
        redrawn_now = len([d for d in env.drawn if d['kind'] == 'total']) - n0
        n_div_now = env.last('total')['n_div']
        await env.settle()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if page.chip.visible:
                break
        return page, within, redrawn_now, n_div_now, list(broker.calls), env.last('total')['n_div']
    page, within, redrawn_now, n_div_now, after, n_div_end = asyncio.run(go())
    assert within == [] and redrawn_now == 1               # drawn immediately from page data
    assert n_div_now == 3 and n_div_end == 3               # not redrawn by the refresh itself
    assert after.count('dividends') == 1 and page.chip.visible   # background refetch -> chip


def test_kick_refresh_only_starts_when_expired_and_idle(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.calls.clear()
        page.tab._kick_refresh()                          # fresh: nothing
        await asyncio.sleep(0.2)
        fresh = list(broker.calls)
        env.clock.t += 61
        page.tab._kick_refresh()
        page.tab._kick_refresh()                          # second kick while one is running
        await asyncio.sleep(0.5)
        return fresh, broker.n('dividends')
    fresh, n = asyncio.run(go())
    assert fresh == [] and n == 1


def test_all_accounts_dropdown_choice_survives_a_reload():
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    tab = AccountGrowthTab.__new__(AccountGrowthTab)
    tab._account_ids, tab._single_account = [1, 2], None
    tab._persist_selection('growth', ['B'])
    assert tab._stored_selection('growth', 'k', ['A', 'B', 'C']) == ['B']


# ---- 6: fingerprint false negatives -----------------------------------------------------------------------

def _fp(divs=None, trades=None, positions=None, balance=None):
    from ba2_trade_platform.ui.pages.overview import rows_fingerprint as data_fingerprint
    base_d = [{'symbol': 'AAA', 'amount': 5.0, 'date': '2026-07-01', 'drip_quantity': None, 'account_id': 1},
              {'symbol': 'AAA', 'amount': 5.0, 'date': '2026-04-01', 'drip_quantity': None, 'account_id': 1}]
    base_t = [{'symbol': 'AAA', 'side': 'BUY', 'qty': 10, 'price': 10.0, 'date': '2025-01-01', 'account_id': 1}]
    base_p = [SimpleNamespace(symbol='AAA', qty=10)]
    base_b = [{'date': '2026-10-02', 'net_liquidating_value': 1000.0, 'cash_balance': 10.0}]
    return data_fingerprint(divs if divs is not None else base_d, trades if trades is not None else base_t,
                            positions if positions is not None else base_p)


@pytest.mark.parametrize('name,kw', [
    ('back-dated swap, same amount', dict(divs=[
        {'symbol': 'AAA', 'amount': 5.0, 'date': '2026-07-01', 'drip_quantity': None, 'account_id': 1},
        {'symbol': 'AAA', 'amount': 5.0, 'date': '2026-03-20', 'drip_quantity': None, 'account_id': 1}])),
    ('symbol correction', dict(divs=[
        {'symbol': 'BBB', 'amount': 5.0, 'date': '2026-07-01', 'drip_quantity': None, 'account_id': 1},
        {'symbol': 'AAA', 'amount': 5.0, 'date': '2026-04-01', 'drip_quantity': None, 'account_id': 1}])),
    ('late DRIP quantity', dict(divs=[
        {'symbol': 'AAA', 'amount': 5.0, 'date': '2026-07-01', 'drip_quantity': 0.3, 'account_id': 1},
        {'symbol': 'AAA', 'amount': 5.0, 'date': '2026-04-01', 'drip_quantity': None, 'account_id': 1}])),
    ('trade side correction', dict(trades=[
        {'symbol': 'AAA', 'side': 'SELL', 'qty': 10, 'price': 10.0, 'date': '2025-01-01', 'account_id': 1}])),
    ('position quantity moved', dict(positions=[SimpleNamespace(symbol='AAA', qty=4),
                                                SimpleNamespace(symbol='BBB', qty=6)])),
])
def test_fingerprint_detects_changes_the_old_summary_missed(name, kw):
    assert _fp(**kw) != _fp(), name


def test_fingerprint_is_order_independent_and_stable():
    from ba2_trade_platform.ui.pages.overview import rows_fingerprint
    a = [{'symbol': 'A', 'amount': 1, 'date': '2026-01-01'}, {'symbol': 'B', 'amount': 2, 'date': '2026-01-02'}]
    assert rows_fingerprint(a, [], []) == rows_fingerprint(list(reversed(a)), [], [])


# ---- follow-ups ----------------------------------------------------------------------------------------------

def test_unknown_trade_sides_are_ignored_by_the_forecast_like_the_timeline():
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab, signed_trade_qty
    from ba2_trade_platform.ui.utils.dividend_forecast import add_months
    assert signed_trade_qty('BUY', 3) == 3 and signed_trade_qty('SELL', 3) == -3
    assert signed_trade_qty('TRANSFER', 3) == 0.0 and signed_trade_qty(None, 3) == 0.0
    d = date(TODAY.year, TODAY.month, 15)
    if d >= TODAY:
        d = add_months(d, -1)
    divs = [{'symbol': 'XYZ', 'account_id': 1, 'amount': 50.0, 'drip_quantity': None,
             'date': datetime(*add_months(d, -k).timetuple()[:3])} for k in range(5)]
    odd = {'symbol': 'XYZ', 'account_id': 1, 'side': 'TRANSFER', 'qty': 90, 'price': 1,
           'date': divs[0]['date']}
    tab = AccountGrowthTab.__new__(AccountGrowthTab)
    out = tab._compute_dividend_forecast(divs, [odd], {(1, 'XYZ'): 100.0})
    assert {round(v['total'], 2) for v in out.values()} == {50.0}


def test_partial_price_download_is_not_asked_again_within_a_broker_ttl(env, monkeypatch):
    import yfinance
    ov = env.ov
    calls = []
    monkeypatch.setattr(yfinance, 'download', lambda syms, **k: calls.append(list(syms)))
    monkeypatch.setattr(ov, '_extract_yf_close_prices', lambda h, s: {'2026-09-01': 1.0} if s == 'AAA' else {})

    async def go():
        tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
        tab._init_data_cache()
        tab._set_range('YTD')
        await tab._ensure_prices(['AAA', 'BBB'])
        await tab._ensure_prices(['AAA', 'BBB'])
        within = len(calls)
        env.clock.t += 61
        await tab._ensure_prices(['AAA', 'BBB'])
        return within, len(calls)
    assert asyncio.run(go()) == (1, 2)


# ---- F3a replacement: the refresh button, behaviourally ---------------------------------------------------

def test_refresh_button_clears_the_cache_and_refetches_everything(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.calls.clear()
        with page.client.content:
            page.refresh(None)
        await env.settle()
        return broker
    broker = asyncio.run(go())
    assert [broker.n(n) for n in ('dividends', 'trades', 'positions', 'balance')] == [1, 1, 1, 1]


# ===================================================================== review 4
def test_failure_on_the_load_path_is_visible_inline_and_toasted_from_the_label_slot(env):
    async def go():
        page = env.build()
        await env.settle()
        env.state.brokers[0].fail = {'dividends'}
        env.clock.t += 120
        with page.client.content:
            page.tab._set_range('1y')
            from nicegui import ui
            await page.tab._load_growth_data(ui.label('x'), ui.column(), 1)   # a bare task: no slot
        return page
    page = asyncio.run(go())
    assert env.notes and all(n['ctx_ok'] for n in env.notes)        # shown: raised inside the label slot
    assert page.tab._refresh_warned is True
    assert 'refresh failed' in page.tab._updated_label.text
    assert 'text-orange-500' in page.tab._updated_label.classes


def test_marker_clears_when_a_fetch_succeeds_again(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.fail = {'dividends'}
        env.clock.t += 300
        await page.timer.callback()
        marked = 'refresh failed' in page.tab._updated_label.text
        broker.fail = set()
        env.clock.t += 300
        await page.timer.callback()
        return page, marked
    page, marked = asyncio.run(go())
    assert marked and 'refresh failed' not in page.tab._updated_label.text
    assert page.tab._refresh_warned is False


def test_one_accounts_transient_empty_keeps_that_accounts_rows_in_all_accounts_mode(env):
    b1, b2 = Broker(aid=1), Broker(aid=2)
    b2.divs = [dict(d, symbol='BBB') for d in b2.divs]
    b2.trades = [dict(t, symbol='BBB') for t in b2.trades]
    b2.positions = [SimpleNamespace(symbol='BBB', qty=10, avg_entry_price=10.0)]
    env.state.brokers, env.state.selected = [b1, b2], None

    async def go():
        page = env.build()
        await env.settle()
        n_div, n_trd = env.last('total')['n_div'], env.last('total')['n_trades']
        b2.fail = {'dividends', 'trades'}
        b1.divs.append({'symbol': 'AAA', 'amount': 9.0, 'date': _dt(TODAY - timedelta(days=1)),
                        'drip_quantity': None})
        env.clock.t += 300
        await page.timer.callback()
        cache = page.tab._broker_cache
        return page, n_div, n_trd, len(cache['dividends']), len(cache['trades'])
    page, n_div, n_trd, now_div, now_trd = asyncio.run(go())
    assert (n_div, n_trd) == (6, 2)
    assert now_div == 7 and now_trd == 2          # acct 1 updated, acct 2 kept its 3 rows + trade
    assert page.chip.visible                      # acct 1's new dividend is a real change


def test_a_failing_broker_is_asked_once_per_ttl_not_once_per_click(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.fail = {'dividends', 'trades', 'positions'}
        broker.calls.clear()
        env.clock.t += 120
        for _ in range(3):
            env.captured['on_scope_change']()
            await asyncio.sleep(0.4)
            env.clock.t += 5
        return broker
    broker = asyncio.run(go())
    assert broker.n('dividends') == 1               # one round for the three clicks


def test_refresh_button_during_an_outage_keeps_the_old_rows_on_screen(env):
    async def go():
        page = env.build()
        await env.settle()
        broker = env.state.brokers[0]
        broker.fail = {'dividends', 'trades', 'positions'}
        with page.client.content:
            page.refresh(None)
        await env.settle()
        return page
    page = asyncio.run(go())
    assert env.last('total')['n_div'] == 3 and env.last('total')['n_trades'] == 1
    assert 'refresh failed' in page.tab._updated_label.text


def test_an_account_whose_balance_history_is_empty_is_asked_once_per_load(env):
    class NoBalance(Broker):
        def get_balance_history(self, start_date=None, end_date=None):
            self._enter('balance')
            return []
    env.state.brokers = [NoBalance()]

    async def go():
        page = env.build()
        await env.settle()
        return env.state.brokers[0]
    assert asyncio.run(go()).n('balance') == 1


def test_a_click_after_the_ttl_keeps_the_all_accounts_symbol_pick_and_does_not_reload(env):
    b1, b2 = Broker(aid=1), Broker(aid=2)
    b1.positions = [SimpleNamespace(symbol=s, qty=10, avg_entry_price=10.0, cost_basis=100.0)
                    for s in ('AAA', 'BBB')]
    b2.positions = [SimpleNamespace(symbol='CCC', qty=10, avg_entry_price=10.0, cost_basis=100.0)]
    env.state.brokers, env.state.selected = [b1, b2], None
    env.restore('_render_per_position_section')

    async def go():
        page = env.build()
        await env.settle()
        sel = [e for e in page.client.elements.values() if type(e).__name__ == 'Select'
               and e._props.get('label') == 'Select Symbol'][-1]
        n_total = len([d for d in env.drawn if d['kind'] == 'total'])
        env.clock.t += 120
        with page.client.content:
            sel.set_value('CCC')
        await env.settle()
        sels = [e for e in page.client.elements.values() if type(e).__name__ == 'Select'
                and e._props.get('label') == 'Select Symbol']
        return page, sels, len([d for d in env.drawn if d['kind'] == 'total']) - n_total
    page, sels, redraws = asyncio.run(go())
    assert redraws == 0                                    # no page reload
    assert [d['symbol'] for d in env.drawn if d['kind'] == 'symbol_chart'][-1] == 'CCC'
    assert sels[-1].value == 'CCC'
    assert page.tab._stored_singles('position_symbol') == ['CCC']


def test_a_checkbox_click_after_the_ttl_keeps_the_choice_and_does_not_reload(env, monkeypatch):
    from datetime import timedelta as td
    monkeypatch.setattr(env.ov, '_extract_yf_close_prices',
                        lambda h, s: {(TODAY - td(days=i)).isoformat(): 10.0 + i * 0.01 for i in range(1, 100)})
    env.restore('_render_growth_by_label_charts')
    env.state.brokers[0].positions = [SimpleNamespace(symbol='AAA', qty=10, avg_entry_price=10.0,
                                                      cost_basis=100.0)]

    async def go():
        page = env.build()
        await env.settle()
        boxes = [e for e in page.client.elements.values()
                 if type(e).__name__ == 'Checkbox' and e.text == 'Dividends']
        assert boxes, 'growth-by-label controls were not rendered'
        n_total = len([d for d in env.drawn if d['kind'] == 'total'])
        env.clock.t += 120
        with page.client.content:
            boxes[-1].set_value(False)
        await env.settle()
        boxes = [e for e in page.client.elements.values()
                 if type(e).__name__ == 'Checkbox' and e.text == 'Dividends']
        return boxes[-1].value, len([d for d in env.drawn if d['kind'] == 'total']) - n_total
    value, redraws = asyncio.run(go())
    assert value is False and redraws == 0
