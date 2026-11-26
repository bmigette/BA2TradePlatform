"""Live Trades "Live View" tab: the thin NiceGUI layer, rendered into a bare NiceGUI client (no browser) with a fake
loader; storage is a dict. Pins the wiring: rows refreshed in place, stale rows of a failed account, the timer rules,
remembered choices."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from ba2_trade_platform.services.live_positions import LiveLoad
from ba2_trade_platform.ui.components import LivePositionsTab as tab_mod
from ba2_trade_platform.ui.utils import live_positions as lp

KEY = 'live_trades_page_state:8080'
NOW = datetime(2026, 11, 26, 10, 0, 0)


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-live-positions'), request=None)
    yield client
    client.remove_elements(client.elements.values())


@pytest.fixture
def storage(monkeypatch):
    store = {}
    monkeypatch.setattr(tab_mod, 'read_stored', lambda key: store.get(key))

    def write(key, **changes):
        store[key] = {**(store.get(key) or {}), **changes}
    monkeypatch.setattr(tab_mod, 'write_choices', write)
    return store


def _pos(symbol, pl_day, pl_open=10.0):
    return {"symbol": symbol, "qty": 1.0, "current_price": 10.0, "avg_entry_price": 9.0, "cost_basis": 9.0,
            "market_value": 10.0, "unrealized_pl": pl_open, "unrealized_plpc": 0.1, "change_today": 0.01,
            "unrealized_intraday_pl": pl_day, "unrealized_intraday_plpc": 0.01, "asset_class": "us_equity"}


class Loader:
    """Each call returns the next scripted answer: ``{account_id: list | 'failed'}``."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, account_id, first_load):
        self.calls.append((account_id, first_load))
        answer = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        reads = []
        for aid, positions in answer.items():
            name = f"Acct{aid}"
            if positions == 'failed':
                reads.append(lp.AccountRead(aid, name, lp.STATUS_FAILED, "the positions read failed"))
            else:
                rows = tuple(lp.position_row(aid, name, p, ()) for p in positions)
                reads.append(lp.AccountRead(aid, name, lp.STATUS_OK, None, rows))
        return LiveLoad(reads=reads, calls={})


def _make(client, loader, account_id=None, active=True, **kw):
    async def run():
        with client:
            tab = tab_mod.LivePositionsTab(loader=loader, account_id_fn=lambda: account_id,
                                           is_active=lambda: active, storage_port=8080, now_fn=lambda: NOW, **kw)
            if tab._task is not None:
                await tab._task
        return tab
    return asyncio.run(run())


def _refresh(client, tab):
    async def run():
        with client:
            await tab._refresh()
    asyncio.run(run())


def test_first_load_fills_the_table_sorted_by_pl_day(nicegui_client, storage):
    loader = Loader({1: [_pos("A", 1.0), _pos("B", 5.0)], 2: [_pos("C", -2.0)]})
    tab = _make(nicegui_client, loader)
    assert [r['symbol'] for r in tab._table.rows] == ["B", "A", "C"]
    assert loader.calls == [(None, True)]
    cols = {c['name']: c for c in tab._table.columns}
    assert cols['pl_day']['total_text'] == "4.00" and 'account' in cols
    assert tab._table._props['pagination']['sortBy'] == 'pl_day'
    assert tab._table._props['pagination']['descending'] is True
    assert tab._updated.text == "Updated 10:00:00"


def test_a_refresh_updates_the_same_table_in_place(nicegui_client, storage):
    loader = Loader({1: [_pos("A", 1.0)]}, {1: [_pos("A", 3.0)]})
    tab = _make(nicegui_client, loader)
    table = tab._table
    _refresh(nicegui_client, tab)
    assert tab._table is table
    assert table.rows[0]['pl_day'] == 3.0
    assert loader.calls[-1] == (None, False)


def test_a_failed_account_keeps_its_rows_stale_with_a_warning(nicegui_client, storage):
    loader = Loader({1: [_pos("A", 1.0)], 2: [_pos("B", 2.0)]}, {1: [_pos("A", 1.0)], 2: 'failed'})
    tab = _make(nicegui_client, loader)
    _refresh(nicegui_client, tab)
    rows = {r['symbol']: r for r in tab._table.rows}
    assert rows['B']['stale'] is True and rows['A']['stale'] is False
    texts = [el.text for el in tab._warnings.default_slot.children]
    assert len(texts) == 1 and "Acct2" in texts[0]
    assert {c['name']: c for c in tab._table.columns}['symbol']['total_text'] == "Total (partial)"


def test_a_failed_account_on_first_load_shows_the_others_and_a_warning(nicegui_client, storage):
    tab = _make(nicegui_client, Loader({1: [_pos("A", 1.0)], 2: 'failed'}))
    assert [r['symbol'] for r in tab._table.rows] == ["A"]
    assert "Acct2" in tab._warnings.default_slot.children[0].text


def test_one_account_has_no_account_column(nicegui_client, storage):
    tab = _make(nicegui_client, Loader({1: [_pos("A", 1.0)]}), account_id=1)
    assert 'account' not in [c['name'] for c in tab._table.columns]


def test_an_inactive_tab_does_not_load_until_shown(nicegui_client, storage):
    loader = Loader({1: [_pos("A", 1.0)]})
    tab = _make(nicegui_client, loader, active=False)
    assert loader.calls == [] and tab._table.rows == []

    async def run():
        with nicegui_client:
            await tab._tick()                         # the timer does nothing on an inactive tab
            assert loader.calls == []
            tab._is_active = lambda: True
            tab.on_shown()
            await tab._task
    asyncio.run(run())
    assert loader.calls == [(None, True)] and len(tab._table.rows) == 1


def test_a_tick_is_skipped_while_a_read_is_running(nicegui_client, storage):
    loader = Loader({1: [_pos("A", 1.0)]})
    tab = _make(nicegui_client, loader)
    tab._client = SimpleNamespace(has_socket_connection=True)
    tab._loading = True

    async def run():
        with nicegui_client:
            await tab._tick()
    asyncio.run(run())
    assert len(loader.calls) == 1


def test_the_interval_and_sort_are_remembered_and_off_stops_the_timer(nicegui_client, storage):
    tab = _make(nicegui_client, Loader({1: [_pos("A", 1.0)]}))
    assert tab._interval.value == "10" and tab._timer.interval == 10.0
    with nicegui_client:
        tab._interval.set_value("off")
    assert storage[KEY]["interval"] == "off" and tab._timer.active is False
    with nicegui_client:
        tab._interval.set_value("5")
    assert tab._timer.active is True and tab._timer.interval == 5.0
    tab._on_sort(SimpleNamespace(value={'sortBy': 'symbol', 'descending': False, 'rowsPerPage': 0}))
    assert storage[KEY]["sort_by"] == "symbol" and storage[KEY]["descending"] is False


def test_stored_choices_are_restored(nicegui_client, storage):
    storage[KEY] = {"tab": "Live View", "interval": "30", "sort_by": "pl_open", "descending": False}
    tab = _make(nicegui_client, Loader({1: [_pos("A", 1.0, pl_open=5.0), _pos("B", 1.0, pl_open=-5.0)]}))
    assert tab._interval.value == "30"
    assert tab._table._props['pagination']['sortBy'] == 'pl_open'
    assert [r['symbol'] for r in tab._table.rows] == ["B", "A"]


def test_the_filter_recomputes_the_total_for_the_rows_shown(nicegui_client, storage):
    tab = _make(nicegui_client, Loader({1: [_pos("AAA", 1.0), _pos("BBB", 5.0)]}))
    with nicegui_client:
        tab._filter.set_value("bbb")
    assert {c['name']: c for c in tab._table.columns}['pl_day']['total_text'] == "5.00"


def test_a_loader_that_raises_ends_in_the_status_line_and_keeps_the_rows_stale(nicegui_client, storage):
    calls = []

    def loader(account_id, first_load):
        calls.append(first_load)
        if len(calls) > 1:
            raise RuntimeError("boom")
        return Loader({1: [_pos("A", 1.0)]})(account_id, first_load)

    tab = _make(nicegui_client, loader)
    _refresh(nicegui_client, tab)
    assert "Could not load" in tab._status.text
    assert tab._table.rows[0]['stale'] is True


def test_the_phone_switch_reorders_the_columns_in_place(nicegui_client, storage):
    tab = _make(nicegui_client, Loader({1: [_pos("A", 1.0)]}))
    table = tab._table
    with nicegui_client:
        tab._set_phone(True)
    assert [c['name'] for c in table.columns][:4] == ["symbol", "pl_day", "pl_open_pct", "pl_open"]
    assert tab._table is table
