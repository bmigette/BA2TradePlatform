"""The label growth simulator on the Portfolio Allocation page: the chart icon on each label's header and the dialog it
opens. Same harness as ``tests/test_portfolio_allocation_page.py`` (a bare NiceGUI ``Client`` slot stack, handlers
driven by hand, ``asyncio.to_thread`` inline); the fetch is replaced, so no network and no broker."""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

import tests.test_portfolio_allocation_page as pagetests
from ba2_trade_platform.core.portfolio_allocation_store import get_symbol_rows, set_managed_label
from ba2_trade_platform.services.errors import PriceSourceUnreachable
from ba2_trade_platform.services.label_growth import LabelGrowthInputs
from ba2_trade_platform.ui.pages import label_growth_dialog as dlg
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.utils import label_growth_sim as sim
from tests.factories import create_account_definition

AS_OF = date(2026, 11, 12)


@pytest.fixture(autouse=True)
def run_to_thread_inline(monkeypatch):
    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', _inline)


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-portfolio-allocation-growth'), request=None)
    yield client
    client.remove_elements(client.elements.values())


@pytest.fixture
def account_id():
    return create_account_definition(name='Manual', provider='MockAccount').id


def _inputs(closes, distributions=None, source='fmp'):
    return LabelGrowthInputs(as_of=AS_OF, start=sim.history_start(AS_OF), closes=closes,
                             distributions=distributions if distributions is not None
                             else {s: sim.DistributionHistory(events=()) for s in closes},
                             price_source=source)


def _fake_fetch(monkeypatch, result):
    """Replace the dialog's fetch; record what it was asked for."""
    calls = []

    def fetch(account_id, symbols, *, as_of, api_key):
        calls.append({'account_id': account_id, 'symbols': list(symbols), 'as_of': as_of, 'api_key': api_key})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(dlg, 'fetch_label_growth_inputs', fetch)
    return calls


async def _settle():
    """Let the dialog's load task (started with create_task on the click) finish."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def _click_and_settle(client, element):
    async def _run():
        with client:
            pagetests._fire(element)
            await _settle()
    asyncio.run(_run())


def _all(client):
    return list(client.layout.descendants()) + [el for el in client.elements.values()]


def _marked_anywhere(client, marker):
    seen, out = set(), []
    for el in _all(client):
        if id(el) not in seen and marker in (el._markers or []):
            seen.add(id(el))
            out.append(el)
    return out


CLOSES = {'AAPL': {'2021-11-12': 100.0, '2025-11-12': 150.0, '2025-12-31': 160.0, '2026-11-11': 200.0},
          'MSFT': {'2021-11-12': 300.0, '2025-11-12': 400.0, '2025-12-31': 420.0, '2026-11-11': 450.0}}


# ---------------------------------------------------------------------------
# the icon
# ---------------------------------------------------------------------------

def test_every_label_header_carries_the_chart_icon(nicegui_client, account_id):
    views = pagetests._views([pagetests.ManagedLabel('ARK26', 40.0), pagetests.ManagedLabel('Income', 30.0)],
                             {'ARK26': ['AAPL', 'MSFT'], 'Income': ['JEPI']},
                             weights={'ARK26': {'AAPL': 50.0, 'MSFT': 50.0}, 'Income': {'JEPI': 100.0}})
    root = pagetests._draw(nicegui_client, account_id, views)
    rows = pagetests._marked(root, page.MARKER_BAR_ROW)
    assert len(rows) == 2
    for row in rows:
        icons = [el for el in row.descendants() if page.MARKER_LABEL_GROWTH_SIM in (el._markers or [])]
        assert len(icons) == 1
        assert icons[0]._props.get('name') == 'show_chart'
        # click.stop, like the pencil: opening the simulator must not fold or unfold the label
        assert any(l.type == 'click' and 'stop' in l.modifiers for l in icons[0]._event_listeners.values()) \
            or any(l.type == 'click.stop' for l in icons[0]._event_listeners.values())
    assert page.LABEL_GROWTH_SIM_TOOLTIP in pagetests._tooltip_texts(root)


def test_the_icon_opens_the_simulator_with_the_default_amount(monkeypatch, nicegui_client, account_id):
    pagetests._fmp_key(monkeypatch)
    calls = _fake_fetch(monkeypatch, _inputs(CLOSES))
    pagetests._freeze_today(monkeypatch, AS_OF)
    root = pagetests._draw(nicegui_client, account_id,
                           pagetests._one_label(account_id, weights={'AAPL': 60.0, 'MSFT': 40.0}))
    icon = pagetests._marked(root, page.MARKER_LABEL_GROWTH_SIM)[0]

    _click_and_settle(nicegui_client, icon)

    dialogs = _marked_anywhere(nicegui_client, dlg.MARKER_SIM_DIALOG)
    assert len(dialogs) == 1 and dialogs[0].value is True                       # opened
    amount = _marked_anywhere(nicegui_client, dlg.MARKER_SIM_AMOUNT)[0]
    assert amount.value == sim.DEFAULT_AMOUNT == 10_000.0
    assert calls == [{'account_id': account_id, 'symbols': ['AAPL', 'MSFT'], 'as_of': AS_OF,
                      'api_key': 'FMP-TEST-KEY'}]
    # four horizons side by side, and no error
    assert len(_marked_anywhere(nicegui_client, dlg.MARKER_SIM_HORIZON_CARD)) == 4
    assert not _marked_anywhere(nicegui_client, dlg.MARKER_SIM_ERROR)
    table = _marked_anywhere(nicegui_client, dlg.MARKER_SIM_TABLE)[0]
    assert [r['symbol'] for r in table.rows] == ['AAPL', 'MSFT']
    assert [r['share'] for r in table.rows] == ['60.00%', '40.00%']


def test_opening_the_simulator_writes_nothing(monkeypatch, nicegui_client, account_id, test_engine):
    """Read-only, PROVEN: every statement the database sees while the icon is clicked and the dialog loads, recomputes
    and switches horizon is a SELECT, and no table changes size -- orders, transactions and allocation rows included.
    A real write anywhere on that path fails this."""
    from sqlalchemy import event, func, select
    from sqlmodel import Session, SQLModel

    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    pagetests._fmp_key(monkeypatch)
    _fake_fetch(monkeypatch, _inputs(CLOSES))
    root = pagetests._draw(nicegui_client, account_id,
                           pagetests._one_label(account_id, weights={'AAPL': 60.0, 'MSFT': 40.0}))

    def counts():
        with Session(test_engine) as session:
            return {t.name: session.exec(select(func.count()).select_from(t)).one()[0]
                    for t in SQLModel.metadata.sorted_tables}

    tables = {t.name for t in SQLModel.metadata.sorted_tables}
    assert {'tradingorder', 'transaction'} <= tables
    assert any('alloc' in t for t in tables)
    before = counts()
    writes = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ''
        if verb not in ('SELECT', 'PRAGMA', 'BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'):
            writes.append(statement)

    event.listen(test_engine, 'before_cursor_execute', spy)
    try:
        _click_and_settle(nicegui_client, pagetests._marked(root, page.MARKER_LABEL_GROWTH_SIM)[0])
        dialog_amount = _marked_anywhere(nicegui_client, dlg.MARKER_SIM_AMOUNT)[0]
        with nicegui_client:
            pagetests._drive_value(dialog_amount, 25_000.0)
            for card in _marked_anywhere(nicegui_client, dlg.MARKER_SIM_HORIZON_CARD)[:1]:
                pagetests._fire(card)
    finally:
        event.remove(test_engine, 'before_cursor_execute', spy)
    assert writes == []
    assert counts() == before


def test_without_an_fmp_key_it_says_so_and_opens_nothing(monkeypatch, nicegui_client, account_id):
    pagetests._fmp_key(monkeypatch, value='')
    sent = pagetests._capture_notifications(monkeypatch)
    calls = _fake_fetch(monkeypatch, _inputs(CLOSES))
    root = pagetests._draw(nicegui_client, account_id, pagetests._one_label(account_id))
    _click_and_settle(nicegui_client, pagetests._marked(root, page.MARKER_LABEL_GROWTH_SIM)[0])
    assert calls == []
    assert not _marked_anywhere(nicegui_client, dlg.MARKER_SIM_DIALOG)
    assert any('FMP_API_KEY' in m and t == 'warning' for m, t in sent)


# ---------------------------------------------------------------------------
# the split it simulates
# ---------------------------------------------------------------------------

def test_the_split_is_the_effective_one_excluded_symbols_out_unset_ones_named():
    live = page._new_live_state(exclusions={'TSLA': object()})
    live['view_by_label']['L'] = SimpleNamespace(rows=[SimpleNamespace(symbol=s)
                                                       for s in ('AAPL', 'MSFT', 'TSLA', 'NVDA')])
    live['weights']['L'] = {'AAPL': 50.0, 'MSFT': 30.0, 'TSLA': 20.0}           # NVDA: no share set
    shares, excluded, unset = page.label_growth_split(live, 'L')
    assert shares == pytest.approx({'AAPL': 62.5, 'MSFT': 37.5})
    assert excluded == ['TSLA']
    assert unset == ['NVDA']
    assert 'TSLA (excluded' in dlg.outside_split_text(excluded, unset)
    assert 'NVDA (no share set)' in dlg.outside_split_text(excluded, unset)


def test_a_share_typed_after_the_page_loaded_is_the_one_simulated(monkeypatch, nicegui_client, account_id):
    """The live weights, not the ones the page was drawn with: an inline edit is in the simulation at once."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    pagetests._fmp_key(monkeypatch)
    _fake_fetch(monkeypatch, _inputs(CLOSES))
    root = pagetests._draw(nicegui_client, account_id,
                           pagetests._one_label(account_id, weights={'AAPL': 50.0, 'MSFT': 50.0}))
    pagetests._emit(pagetests._tables(root)[0], 'weightChange', ['AAPL', 30.0])
    _click_and_settle(nicegui_client, pagetests._marked(root, page.MARKER_LABEL_GROWTH_SIM)[0])
    table = _marked_anywhere(nicegui_client, dlg.MARKER_SIM_TABLE)[0]
    assert {r['symbol']: r['share'] for r in table.rows} == {'AAPL': '30.00%', 'MSFT': '50.00%'}


# ---------------------------------------------------------------------------
# the dialog itself
# ---------------------------------------------------------------------------

def _open(client, shares, fetch):
    holder = {}

    async def _run():
        with client:
            holder['d'] = dlg.open_label_growth(1, 'ARK26', shares, api_key='K', as_of=AS_OF, fetch=fetch)
            await holder['d'].load_task
    asyncio.run(_run())
    return holder['d']


def _texts(client):
    return [el._text for el in _all(client) if getattr(el, '_text', None)]


def test_a_provider_error_is_shown_in_the_dialog(nicegui_client, monkeypatch):
    errors = []
    monkeypatch.setattr(dlg.logger, 'error', lambda msg, *a, **k: errors.append(str(msg)))

    def fetch(*a, **k):
        raise PriceSourceUnreachable('The price source is busy: every download slot is in use', {'busy': True})

    _open(nicegui_client, {'AAPL': 100.0}, fetch)
    assert _marked_anywhere(nicegui_client, dlg.MARKER_SIM_ERROR)
    assert any('every download slot is in use' in t for t in _texts(nicegui_client))
    assert errors


def test_a_split_over_100_is_refused_before_anything_is_fetched(nicegui_client):
    calls = []
    _open(nicegui_client, {'AAPL': 70.0, 'MSFT': 40.0}, lambda *a, **k: calls.append(1))
    assert calls == []
    assert any('over 100%' in t for t in _texts(nicegui_client))


def test_changing_the_amount_recomputes_without_refetching(nicegui_client):
    calls = []

    def fetch(*a, **k):
        calls.append(1)
        return _inputs(CLOSES)

    d = _open(nicegui_client, {'AAPL': 100.0}, fetch)
    assert d.result.horizons['1y'].price_value == pytest.approx(10_000.0 / 150.0 * 200.0)
    with nicegui_client:
        pagetests._drive_value(d.amount_input, 30_000.0)
    assert d.result.horizons['1y'].price_value == pytest.approx(30_000.0 / 150.0 * 200.0)
    assert calls == [1]
    with nicegui_client:
        pagetests._drive_value(d.amount_input, None)
    assert any('positive amount' in t for t in _texts(nicegui_client))


def test_late_and_unmeasurable_symbols_are_visible_in_the_dialog(nicegui_client):
    closes = {'AAPL': CLOSES['AAPL'], 'NEW': {'2024-06-03': 10.0, '2026-11-11': 12.0}}
    d = _open(nicegui_client, {'AAPL': 40.0, 'NEW': 40.0, 'GONE': 20.0},
              lambda *a, **k: _inputs(closes, {'AAPL': sim.DistributionHistory(events=()),
                                               'NEW': sim.DistributionHistory(events=())}))
    with nicegui_client:
        d._select('3y')
    texts = _texts(nicegui_client)
    assert any('1 of 3 symbols did not exist for the whole period' in t for t in texts)
    assert any('GONE' in t and 'held as cash' in t for t in texts)
    table = _marked_anywhere(nicegui_client, dlg.MARKER_SIM_TABLE)[-1]
    by = {r['symbol']: r for r in table.rows}
    assert by['NEW']['status'] == 'Late start' and '2024-06-03' in by['NEW']['note']
    assert by['GONE']['status'] == 'Unmeasurable (cash)'


# ---------------------------------------------------------------------------
# review 2026-11-12
# ---------------------------------------------------------------------------

def test_a_failure_while_drawing_the_results_is_shown_not_left_on_the_spinner(nicegui_client, monkeypatch):
    errors = []
    monkeypatch.setattr(dlg.logger, 'error', lambda msg, *a, **k: errors.append(str(msg)))

    def broken(h):
        raise RuntimeError('chart options broke')

    monkeypatch.setattr(dlg.sim, 'growth_chart_options', broken)
    d = _open(nicegui_client, {'AAPL': 100.0}, lambda *a, **k: _inputs(CLOSES))
    assert _marked_anywhere(nicegui_client, dlg.MARKER_SIM_ERROR)
    assert any('chart options broke' in t for t in _texts(nicegui_client))
    assert not any('Loading' in t for t in _texts(nicegui_client))
    assert errors
    assert d.load_task.done() and d.load_task.exception() is None


def test_a_failure_on_an_amount_change_is_shown_too(nicegui_client, monkeypatch):
    d = _open(nicegui_client, {'AAPL': 100.0}, lambda *a, **k: _inputs(CLOSES))
    monkeypatch.setattr(dlg.logger, 'error', lambda msg, *a, **k: None)
    monkeypatch.setattr(dlg.sim, 'table_rows', lambda h: (_ for _ in ()).throw(KeyError('row')))
    with nicegui_client:
        pagetests._drive_value(d.amount_input, 12_000.0)
    assert _marked_anywhere(nicegui_client, dlg.MARKER_SIM_ERROR)


def test_a_dialog_closed_during_the_fetch_finishes_quietly(nicegui_client, monkeypatch):
    """The browser tab closed (or the dialog dismissed) while the prices were downloading: the load must end without
    drawing into deleted elements and without an exception left on the task."""
    errors = []
    monkeypatch.setattr(dlg.logger, 'error', lambda msg, *a, **k: errors.append(str(msg)))
    holder = {}

    def fetch(*a, **k):
        holder['d'].dialog.delete()
        return _inputs(CLOSES)

    async def _run():
        with nicegui_client:
            holder['d'] = dlg.open_label_growth(1, 'ARK26', {'AAPL': 100.0}, api_key='K', as_of=AS_OF,
                                                fetch=fetch)
            await asyncio.gather(holder['d'].load_task, return_exceptions=True)
    asyncio.run(_run())
    task = holder['d'].load_task
    assert task.done() and task.exception() is None
    assert holder['d'].result is None
    assert errors == []


def test_shown_and_logged_errors_are_redacted(nicegui_client, monkeypatch):
    errors = []
    monkeypatch.setattr(dlg.logger, 'error', lambda msg, *a, **k: errors.append(str(msg)))

    def fetch(*a, **k):
        raise RuntimeError('GET https://data.example/v2/bars?apikey=SECRET123&token=TOKEN456 failed')

    _open(nicegui_client, {'AAPL': 100.0}, fetch)
    shown = ' '.join(_texts(nicegui_client))
    assert 'failed' in shown
    for secret in ('SECRET123', 'TOKEN456'):
        assert secret not in shown
        assert not any(secret in m for m in errors)


def test_closing_the_dialog_deletes_it(nicegui_client):
    d = _open(nicegui_client, {'AAPL': 100.0}, lambda *a, **k: _inputs(CLOSES))
    with nicegui_client:
        d.close()
    assert d.dialog.is_deleted


def test_the_card_shows_a_data_gap(nicegui_client):
    closes = {'AAPL': CLOSES['AAPL'], 'HALT': {'2025-09-01': 50.0, '2026-11-11': 60.0}}
    _open(nicegui_client, {'AAPL': 50.0, 'HALT': 50.0}, lambda *a, **k: _inputs(closes))
    texts = _texts(nicegui_client)
    assert any('data gap' in t.lower() for t in texts)
