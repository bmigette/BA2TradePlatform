"""Overview "Heatmap" tab: the thin NiceGUI layer. Rendered into a bare NiceGUI client (no browser) with a fake
loader; storage is replaced by a dict, so these pin the wiring: what is restored, what is offered, what is saved, and
that every load ends in a visible state."""
import asyncio
import threading
from datetime import date, datetime, timezone

import pytest

from ba2_trade_platform.services.position_heatmap import HeatmapData
from ba2_trade_platform.ui.components import PositionHeatmapTab as tab_mod
from ba2_trade_platform.ui.utils import position_heatmap as hm
from ba2_trade_platform.ui.utils.position_heatmap import Fill, Lot, MarketInputs, NO_EXPERT

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)        # Tuesday, 10:00 ET
KEY = 'overview_heatmap_state:8080'


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-position-heatmap'), request=None)
    yield client
    client.remove_elements(client.elements.values())


@pytest.fixture
def storage(monkeypatch):
    store = {}
    monkeypatch.setattr(tab_mod, '_read_stored', lambda key: store.get(key))
    monkeypatch.setattr(tab_mod, '_write_stored', lambda key, value: store.__setitem__(key, value))
    return store


def _buy(qty, price, day):
    return Fill('BUY', qty, price, datetime(day.year, day.month, day.day, 15, tzinfo=timezone.utc))


def _data():
    lots = [Lot(1, 'AAPL', 'Alpha-1', 'long', 10.0, (_buy(10.0, 90.0, date(2026, 1, 5)),), account_id=1),
            Lot(2, 'MSFT', NO_EXPERT, 'long', 2.0, (_buy(2.0, 300.0, date(2026, 1, 5)),), account_id=1),
            Lot(3, 'SPY', 'Alpha-1', 'long', 1.0, (), is_option=True, account_id=1)]
    market = MarketInputs(prices={'AAPL': 110.0, 'MSFT': 310.0}, price_missing={},
                          closes={'AAPL': {'2026-11-23': 100.0}, 'MSFT': {'2026-11-23': 300.0}}, closes_missing={},
                          market_caps={'AAPL': 3e12}, market_caps_missing={'MSFT': 'FMP returned no market cap'},
                          labels={'AAPL': ['tech'], 'MSFT': []}, broker_qty={1: {'AAPL': 12.0, 'MSFT': 2.0}})
    return HeatmapData(lots=lots, market=market, calls={})


def _render(client, loader=None, **kw):
    async def run():
        with client:
            tab = tab_mod.PositionHeatmapTab(loader=loader or (lambda aid, now, timing: _data()),
                                             now_fn=lambda: NOW, account_id_fn=lambda: None,
                                             storage_port=8080, **kw)
            await tab._task
        return tab
    return asyncio.run(run())


def _texts(container):
    return [el.text for el in container.descendants() if hasattr(el, 'text')]


def test_the_tab_offers_the_loaded_experts_and_labels(nicegui_client, storage):
    tab = _render(nicegui_client)
    assert list(tab._expert_sel.options) == ['Alpha-1', NO_EXPERT]
    assert list(tab._label_sel.options) == ['tech', hm.UNLABELED]
    assert tab._tf.value == 'today' and tab._metric.value == 'price' and tab._size.value == 'company'


def test_the_tab_restores_stored_choices_and_drops_an_expert_that_is_gone(nicegui_client, storage):
    storage[KEY] = {'timeframe': 'month', 'metric': 'pnl', 'size': 'exposure',
                    'experts': ['Gone-9', 'Alpha-1'], 'labels': ['gone']}
    tab = _render(nicegui_client)
    assert (tab._tf.value, tab._metric.value, tab._size.value) == ('month', 'pnl', 'exposure')
    assert tab._expert_sel.value == ['Alpha-1']
    assert tab._label_sel.value == []                     # nothing left = all
    assert storage[KEY]['experts'] == ['Gone-9', 'Alpha-1']   # not rewritten by a load


def test_a_filter_that_does_not_exist_here_is_named_on_screen(nicegui_client, storage):
    storage[KEY] = {'experts': ['Gone-9'], 'labels': ['gone']}
    tab = _render(nicegui_client)
    assert 'Gone-9' in tab._filter_note.text and 'gone' in tab._filter_note.text
    assert 'not in this account' in tab._filter_note.text


def test_a_change_is_remembered_and_keeps_other_accounts_experts(nicegui_client, storage):
    storage[KEY] = {'experts': ['Gone-9']}
    tab = _render(nicegui_client)
    with nicegui_client:
        tab._tf.value = 'year'
        tab._expert_sel.value = [NO_EXPERT]
    saved = storage[KEY]
    assert saved['timeframe'] == 'year'
    assert saved['experts'] == [NO_EXPERT, 'Gone-9']


def test_the_caption_issue_list_and_quantity_differences_are_drawn(nicegui_client, storage):
    tab = _render(nicegui_client)
    assert '1 option position excluded' in tab._caption.text
    assert 'opened through the platform' in tab._caption.text
    assert any('MSFT: no market cap' in t for t in _texts(tab._issues_box))
    assert any('AAPL' in t and '10' in t and '12' in t for t in _texts(tab._mismatch_box))


def test_a_failed_load_says_so_and_redacts_secrets(nicegui_client, storage):
    def boom(aid, now, timing):
        raise ConnectionError('GET https://x/api?apikey=SECRET123 failed')

    tab = _render(nicegui_client, loader=boom)
    assert 'ConnectionError' in tab._status.text
    assert 'SECRET123' not in tab._status.text


def test_a_failure_after_the_load_still_ends_in_a_visible_status(nicegui_client, storage, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError('calendar unavailable')

    monkeypatch.setattr(tab_mod.hm, 'build_view', broken)
    tab = _render(nicegui_client)
    assert 'RuntimeError' in tab._status.text
    assert tab._reload_btn.enabled


def test_a_load_that_never_returns_ends_in_a_timeout_status(nicegui_client, storage, monkeypatch):
    monkeypatch.setattr(tab_mod, 'LOAD_DEADLINE_S', 0.2)
    release = threading.Event()

    def hang(aid, now, timing):
        release.wait(1)          # asyncio.run waits for this worker at shutdown: keep it short
        return _data()

    try:
        tab = _render(nicegui_client, loader=hang)
    finally:
        release.set()
    assert 'did not finish' in tab._status.text
    assert tab._reload_btn.enabled


def test_reload_while_loading_says_so_and_the_button_is_disabled(nicegui_client, storage):
    release = threading.Event()

    def slow(aid, now, timing):
        release.wait(5)
        return _data()

    async def run():
        with nicegui_client:
            tab = tab_mod.PositionHeatmapTab(loader=slow, now_fn=lambda: NOW, account_id_fn=lambda: None,
                                             storage_port=8080)
            await asyncio.sleep(0.05)
            disabled_while_loading = not tab._reload_btn.enabled
            first = tab._task
            tab._reload()
            said = tab._status.text
            same_task = tab._task is first
            release.set()
            await tab._task
        return tab, disabled_while_loading, said, same_task

    tab, disabled_while_loading, said, same_task = asyncio.run(run())
    assert disabled_while_loading and same_task
    assert 'Already loading' in said
    assert tab._reload_btn.enabled
