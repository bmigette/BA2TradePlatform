"""The Account Growth tab paints from the stored snapshot, then refreshes from the broker in the background.

Drives the REAL ``render()`` closures (``tests.test_overview_refresh_review.env``) against a FILE database, because
the snapshot is read and written off the event loop and the shared in-memory test database is private to the thread
that opened it (``conftest.test_engine``)."""
import asyncio
import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import SQLModel, create_engine

from ba2_trade_platform.core.models import Position
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.services import growth_snapshot as gs
from tests.test_overview_refresh_review import Broker, _dt, env  # noqa: F401  (fixture)

UTC = timezone.utc
TODAY = date.today()


def _position(symbol, qty):
    return Position(asset_class="Equity", avg_entry_price=10.0, avg_entry_swap_rate=None, change_today=0.0,
                    cost_basis=10.0 * qty, current_price=11.0, exchange="", lastday_price=11.0,
                    market_value=11.0 * qty, qty=qty, qty_available=qty, side=OrderDirection.BUY, swap_rate=None,
                    symbol=symbol, unrealized_intraday_pl=0.0, unrealized_intraday_plpc=0.0, unrealized_pl=qty,
                    unrealized_plpc=0.1)


class PosBroker(Broker):
    """The review's counting broker, with REAL ``Position`` objects (what the snapshot codec stores) and a gate that
    can hold every read (to look at the page while the background refresh is in flight)."""

    def __init__(self, aid=1):
        super().__init__(aid=aid)
        self.positions = [_position('AAA', 10.0)]
        self.gate = threading.Event()
        self.gate.set()

    def _enter(self, name):
        self.gate.wait(10)
        super()._enter(name)


@pytest.fixture
def file_db(tmp_path):
    """Every thread sees the same database (a file), with every table of the metadata."""
    import ba2_common.core.db as pkg_db
    engine = create_engine(f"sqlite:///{tmp_path / 'snap.sqlite'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    saved = pkg_db._engine
    pkg_db._engine = engine
    try:
        yield engine
    finally:
        gs.join_dividend_history_refresh(timeout=30)
        pkg_db._engine = saved
        engine.dispose()


def _rows_as_fetched(broker, aid=1, name=None):
    """What ``_fetch_accounts`` keeps for the broker's current rows (account id / name stamped on)."""
    name = name or f'A{aid}'
    divs = [dict(d, account_name=name, account_id=aid) for d in broker.divs]
    trades = [dict(t, account_id=aid) for t in broker.trades]
    balance = [dict(b, account_name=name, account_id=aid) for b in broker.get_balance_history()]
    broker.calls.clear()
    return divs, trades, balance


def _seed(broker, *, aid=1, age=timedelta(minutes=10), floor=None, **override):
    divs, trades, balance = _rows_as_fetched(broker, aid)
    data = dict(dividends=divs, trades=trades, positions=list(broker.positions), balance=balance,
                balance_floor=floor, fetched_at=datetime.now(UTC) - age)
    data.update(override)
    gs.save_growth_snapshot(aid, **data)
    return data


def _draws(env):  # noqa: F811
    return [d for d in env.drawn if d['kind'] == 'total']


@pytest.fixture
def page_env(env, file_db, monkeypatch):  # noqa: F811
    """``env`` with: brokers that return Positions, a draw hook that records what the broker had been asked when the
    chart was drawn, and a settle that also waits for the background refresh."""
    env.state.brokers = [PosBroker()]
    original_total = env.ov.AccountGrowthTab._render_total_growth_chart

    def total(self, bal, divs, trades):
        original_total(self, bal, divs, trades)
        env.drawn[-1]['calls_before'] = sum(len(b.calls) for b in env.state.brokers)
    monkeypatch.setattr(env.ov.AccountGrowthTab, '_render_total_growth_chart', total)

    async def settle():
        for _ in range(300):
            await asyncio.sleep(0.05)
            busy = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                    and any(k in repr(t.get_coro()) for k in ('load_growth', 'background_refresh'))]
            if not busy:
                return
        raise AssertionError('page never settled')
    env.settle = settle
    return env


# ---- a: paint from the snapshot, no broker call before the first draw ----------------------------------------------

def test_a_stored_snapshot_is_drawn_without_any_broker_call_then_refreshed_in_the_background(page_env):
    broker = page_env.state.brokers[0]
    seeded = _seed(broker)

    async def go():
        page = page_env.build()
        await page_env.settle()
        return page
    page = asyncio.run(go())
    draws = _draws(page_env)
    assert draws[0]['calls_before'] == 0                         # painted from the snapshot
    assert sorted(set(broker.calls)) == ['balance', 'dividends', 'positions', 'trades']   # then a FULL refresh
    assert len(draws) == 1                                       # identical data: nothing redrawn
    assert not page.chip.visible
    stored = gs.load_growth_snapshot(1)
    assert stored.fetched_at > seeded['fetched_at']              # the refresh stored its own fetch


def test_the_refresh_after_a_snapshot_paint_redraws_once_when_the_rows_changed(page_env):
    broker = page_env.state.brokers[0]
    _seed(broker)
    broker.divs.append({'symbol': 'AAA', 'amount': 7.0, 'date': _dt(TODAY - timedelta(days=2)), 'drip_quantity': None})

    async def go():
        page = page_env.build()
        await page_env.settle()
        return page
    page = asyncio.run(go())
    draws = _draws(page_env)
    assert [d['n_div'] for d in draws] == [3, 4]                 # snapshot, then the new row, drawn by itself
    assert broker.n('dividends') == 1                            # the redraw came from the cache
    assert not page.chip.visible
    assert len(gs.load_growth_snapshot(1).dividends) == 4


def test_the_refresh_after_a_snapshot_paint_redraws_when_only_the_balance_moved(page_env):
    broker = page_env.state.brokers[0]
    _seed(broker)
    broker.nlv = 2000.0

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    assert [d['nlv'] for d in _draws(page_env)] == [1000.0, 2000.0]


def test_only_the_first_refresh_redraws_by_itself_later_ones_offer_the_chip(page_env):
    broker = page_env.state.brokers[0]
    _seed(broker)

    async def go():
        page = page_env.build()
        await page_env.settle()
        n0 = len(_draws(page_env))
        broker.divs.append({'symbol': 'AAA', 'amount': 7.0, 'date': _dt(TODAY - timedelta(days=1)),
                            'drip_quantity': None})
        page_env.clock.t += 300
        await page.timer.callback()
        return page, n0
    page, n0 = asyncio.run(go())
    assert len(_draws(page_env)) == n0 and page.chip.visible


def test_a_fresh_snapshot_needs_no_refresh_at_all(page_env):
    broker = page_env.state.brokers[0]
    _seed(broker, age=timedelta(seconds=5))

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    assert broker.calls == [] and len(_draws(page_env)) == 1


def test_the_age_label_shows_the_date_of_a_snapshot_from_an_earlier_day(page_env):
    broker = page_env.state.brokers[0]
    seeded = _seed(broker, age=timedelta(days=2))
    broker.gate.clear()                                          # hold the background refresh

    async def go():
        page = page_env.build()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if _draws(page_env):
                break
        await asyncio.sleep(0.1)
        text = page.tab._updated_label.text
        broker.gate.set()
        await page_env.settle()
        return page, text
    page, text = asyncio.run(go())
    local = seeded['fetched_at'].astimezone().replace(tzinfo=None)
    assert text == f"Updated {local.strftime('%Y-%m-%d %H:%M')}"
    assert page.tab._updated_label.text.startswith('Updated ') and '-' not in page.tab._updated_label.text[8:]


def test_the_age_label_of_data_fetched_today_shows_the_time_only():
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    from types import SimpleNamespace
    tab = AccountGrowthTab.__new__(AccountGrowthTab)
    tab._init_data_cache()
    label = SimpleNamespace(text=None, set_text=lambda t: setattr(label, 'text', t), classes=lambda **k: None)
    tab._updated_label = label
    tab._drawn_wall = datetime.now().replace(hour=9, minute=5, second=7)
    tab._update_age_label()
    assert label.text == 'Updated 09:05:07'
    tab._drawn_wall = datetime(2026, 1, 2, 9, 5, 7)
    tab._update_age_label()
    assert label.text == 'Updated 2026-01-02 09:05'


# ---- b: a longer range than the snapshot's balance floor still waits for the longer balance history ---------------

def test_a_range_that_needs_a_longer_balance_history_than_the_snapshot_fetches_it_first(page_env, monkeypatch):
    monkeypatch.setattr(page_env.ov, 'read_range', lambda: 'Max')
    broker = page_env.state.brokers[0]
    _seed(broker, floor=None)                                    # the default ~1y window

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    first = _draws(page_env)[0]
    assert first['calls_before'] == 1 and broker.calls[0] == 'balance'


def test_a_snapshot_whose_floor_already_reaches_back_far_enough_needs_no_balance_fetch(page_env, monkeypatch):
    monkeypatch.setattr(page_env.ov, 'read_range', lambda: 'Max')
    broker = page_env.state.brokers[0]
    _seed(broker, floor=TODAY - timedelta(days=4000))

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    assert _draws(page_env)[0]['calls_before'] == 0


# ---- no snapshot / not every account has one: today's blocking fetch -----------------------------------------------

def test_without_a_snapshot_the_load_waits_for_the_broker_and_then_stores_one(page_env):
    broker = page_env.state.brokers[0]
    assert gs.load_growth_snapshot(1) is None

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    assert _draws(page_env)[0]['calls_before'] == 4
    stored = gs.load_growth_snapshot(1)
    assert stored is not None and len(stored.dividends) == 3 and stored.positions[0].symbol == 'AAA'
    assert stored.balance_floor is None
    assert gs.load_dividends(1).rows == stored.dividends


def test_when_one_of_two_accounts_has_no_snapshot_the_load_waits_for_the_broker(page_env):
    page_env.state.brokers = [PosBroker(aid=1), PosBroker(aid=2)]
    page_env.state.selected = None                               # All accounts
    _seed(page_env.state.brokers[0], aid=1)

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    assert _draws(page_env)[0]['calls_before'] == 8
    assert gs.load_growth_snapshot(2) is not None                # both stored now


def test_two_accounts_with_snapshots_paint_at_once_and_the_stamp_is_the_older_fetch(page_env):
    page_env.state.brokers = [PosBroker(aid=1), PosBroker(aid=2)]
    page_env.state.selected = None
    older = _seed(page_env.state.brokers[0], aid=1, age=timedelta(minutes=30))
    _seed(page_env.state.brokers[1], aid=2, age=timedelta(minutes=5))
    for b in page_env.state.brokers:
        b.gate.clear()                                           # hold the background refresh

    async def go():
        page = page_env.build()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if _draws(page_env):
                break
        wall = page.tab._drawn_wall
        for b in page_env.state.brokers:
            b.gate.set()
        await page_env.settle()
        return wall
    wall = asyncio.run(go())
    assert _draws(page_env)[0]['calls_before'] == 0
    assert wall == older['fetched_at'].astimezone().replace(tzinfo=None)


# ---- d: a failed dataset never overwrites the snapshot ----------------------------------------------------------

def test_a_refresh_with_a_failed_dataset_does_not_overwrite_the_snapshot(page_env):
    broker = page_env.state.brokers[0]
    seeded = _seed(broker)
    broker.fail = {'trades'}                                     # [] after rows = an error-shaped answer

    async def go():
        page = page_env.build()
        await page_env.settle()
        return page
    page = asyncio.run(go())
    assert broker.n('trades') == 1
    stored = gs.load_growth_snapshot(1)
    assert stored.fetched_at == seeded['fetched_at']
    assert len(stored.trades) == 1
    assert len(_draws(page_env)) == 1                            # no automatic redraw after a failed refresh


def test_a_snapshot_that_cannot_be_saved_is_a_warning_and_the_page_still_draws(page_env, monkeypatch):
    warnings = []
    monkeypatch.setattr(page_env.ov.logger, 'warning', lambda msg, *a, **k: warnings.append(str(msg)))

    def broken(*a, **k):
        raise RuntimeError('disk full')
    monkeypatch.setattr(gs, 'save_growth_snapshot', broken)

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    assert len(_draws(page_env)) == 1
    assert any('disk full' in w and 'account 1' in w for w in warnings), warnings


def test_an_unreadable_snapshot_falls_back_to_the_blocking_fetch(page_env, monkeypatch):
    broker = page_env.state.brokers[0]
    _seed(broker)
    monkeypatch.setattr(gs, 'GROWTH_SNAPSHOT_SCHEMA_VERSION', 999)       # the stored row is now another version

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    assert _draws(page_env)[0]['calls_before'] == 4


# ---- f: the load's timing line names the snapshot and the four maths steps -------------------------------------------

def test_the_timing_line_names_the_snapshot_and_the_four_compute_steps(page_env, monkeypatch):
    from ba2_trade_platform.services import load_timing
    lines = []
    monkeypatch.setattr(load_timing.logger, 'info', lambda msg, *a, **k: lines.append(msg))
    _seed(page_env.state.brokers[0], age=timedelta(seconds=240))

    async def go():
        page_env.build()
        await page_env.settle()
    asyncio.run(go())
    line = next(x for x in lines if x.startswith('[PERF:page:load] AccountGrowth('))
    assert 'snapshot ' in line and '1 account(s), 24' in line and 's old]' in line
    for part in ('monthly realized', 'value denominators', 'scope inputs', 'dividend forecast'):
        assert f'{part} ' in line, (part, line)
    assert 'compute ' not in line
