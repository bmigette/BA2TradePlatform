"""The rebalance tester on the Portfolio Allocation page: the icon on each label header, the whole-account button under
the label list, and the dialog. Same harness as the growth simulator's UI tests (a bare NiceGUI ``Client``, handlers
driven by hand, ``asyncio.to_thread`` inline); the fetch is replaced, so no network and no broker."""
import asyncio
from datetime import date

import pandas as pd
import pytest

import tests.test_portfolio_allocation_page as pagetests
from ba2_trade_platform.core.portfolio_allocation_store import set_managed_label
from ba2_trade_platform.services.errors import PriceSourceUnreachable
from ba2_trade_platform.services.label_growth import LabelGrowthInputs
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.pages import rebalance_tester_dialog as dlg
from ba2_trade_platform.ui.utils import label_growth_sim as gsim
from ba2_trade_platform.ui.utils import rebalance_tester_sim as sim
from tests.factories import create_account_definition
from tests.test_portfolio_allocation_label_growth_ui import (  # noqa: F401 -- fixtures
    _all, _marked_anywhere, _settle, account_id, nicegui_client, run_to_thread_inline)

AS_OF = date(2026, 11, 12)
DAYS = pd.bdate_range('2020-01-01', '2026-11-11')


def _flat(price):
    return {d.date().isoformat(): price for d in DAYS}


def _rising(start, per_day):
    return {d.date().isoformat(): start + i * per_day for i, d in enumerate(DAYS)}


CLOSES = {'AAPL': _rising(100.0, 0.05), 'MSFT': _flat(300.0), 'JEPI': _rising(50.0, 0.01)}


def _inputs_for(symbols, closes=CLOSES):
    have = {s: closes[s] for s in symbols if s in closes}
    return LabelGrowthInputs(as_of=AS_OF, start=gsim.history_start(AS_OF), closes=have,
                             distributions={s: gsim.DistributionHistory(events=()) for s in have},
                             price_source='fmp')


def _recording_fetch(closes=CLOSES):
    calls = []

    def fetch(account_id, symbols, *, as_of, api_key):
        calls.append(sorted(symbols))
        return _inputs_for(symbols, closes)
    return calls, fetch


def _two_labels(account_id):
    return pagetests._views([pagetests.ManagedLabel('ARK26', 60.0), pagetests.ManagedLabel('Income', 40.0)],
                            {'ARK26': ['AAPL', 'MSFT'], 'Income': ['JEPI', 'MSFT']},
                            weights={'ARK26': {'AAPL': 50.0, 'MSFT': 50.0}, 'Income': {'JEPI': 75.0, 'MSFT': 25.0}})


def _click(client, element):
    async def _run():
        with client:
            pagetests._fire(element)
            await _settle()
    asyncio.run(_run())


def _texts(client):
    return [el._text for el in _all(client) if getattr(el, '_text', None)]


def _open(client, label_shares, targets, scope, fetch):
    holder = {}

    async def _run():
        with client:
            holder['d'] = dlg.open_rebalance_tester(1, label_shares, targets, scope=scope, api_key='K', as_of=AS_OF,
                                                    fetch=fetch)
            await holder['d'].load_task
    asyncio.run(_run())
    return holder['d']


def _settle_in(client, coro_factory):
    async def _run():
        with client:
            coro_factory()
            await _settle()
    asyncio.run(_run())


# ---------------------------------------------------------------------------
# the entry points
# ---------------------------------------------------------------------------

def test_every_label_header_carries_the_rebalance_icon_and_the_list_the_account_button(nicegui_client, account_id):
    root = pagetests._draw(nicegui_client, account_id, _two_labels(account_id))
    rows = pagetests._marked(root, page.MARKER_BAR_ROW)
    assert len(rows) == 2
    for row in rows:
        icons = [el for el in row.descendants() if page.MARKER_LABEL_REBAL_TEST in (el._markers or [])]
        assert len(icons) == 1
        assert any(l.type == 'click' and 'stop' in l.modifiers for l in icons[0]._event_listeners.values()) \
            or any(l.type == 'click.stop' for l in icons[0]._event_listeners.values())
    assert page.LABEL_REBAL_TEST_TOOLTIP in pagetests._tooltip_texts(root)
    assert len(pagetests._marked(root, page.MARKER_ACCOUNT_REBAL_TEST)) == 1


def test_the_label_icon_opens_the_tester_on_that_label(monkeypatch, nicegui_client, account_id):
    pagetests._fmp_key(monkeypatch)
    pagetests._freeze_today(monkeypatch, AS_OF)
    calls, fetch = _recording_fetch()
    monkeypatch.setattr(dlg, 'fetch_label_growth_inputs', fetch)
    root = pagetests._draw(nicegui_client, account_id, _two_labels(account_id))
    icon = pagetests._marked(root, page.MARKER_LABEL_REBAL_TEST)[1]          # Income
    _click(nicegui_client, icon)
    dialogs = _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_DIALOG)
    assert len(dialogs) == 1 and dialogs[0].value is True
    assert calls == [['JEPI', 'MSFT']]
    assert _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_SCOPE)[0].value == 'Income'
    table = _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_TABLE)[0]
    assert [r['variant'] for r in table.rows] == [sim.VARIANT_NAMES[k] for k in (sim.HOLD, 1, 3, 6, 12)]
    assert _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_CHART)
    assert not _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_ERROR)


def test_the_account_button_opens_the_whole_account_scope(monkeypatch, nicegui_client, account_id):
    pagetests._fmp_key(monkeypatch)
    pagetests._freeze_today(monkeypatch, AS_OF)
    calls, fetch = _recording_fetch()
    monkeypatch.setattr(dlg, 'fetch_label_growth_inputs', fetch)
    seen = {}
    real_open = page.open_rebalance_tester

    def spy(*a, **k):
        seen['dialog'] = real_open(*a, **k)
        return seen['dialog']

    monkeypatch.setattr(page, 'open_rebalance_tester', spy)
    root = pagetests._draw(nicegui_client, account_id, _two_labels(account_id))
    button = pagetests._marked(root, page.MARKER_ACCOUNT_REBAL_TEST)[0]
    assert button._props.get('label') == page.ACCOUNT_REBAL_TEST_LABEL
    _click(nicegui_client, button)
    d = seen['dialog']
    assert d.scope == dlg.ACCOUNT_SCOPE
    assert calls == [['AAPL', 'JEPI', 'MSFT']]
    weights, _ = d.scope_weights(dlg.ACCOUNT_SCOPE)
    assert weights == pytest.approx({'AAPL': 30.0, 'MSFT': 30.0 + 10.0, 'JEPI': 30.0})
    assert d.result is not None and d.result.refusal is None
    assert d.result.window.weights == pytest.approx({'AAPL': 0.3, 'MSFT': 0.4, 'JEPI': 0.3})


def test_without_an_fmp_key_it_says_so_and_opens_nothing(monkeypatch, nicegui_client, account_id):
    pagetests._fmp_key(monkeypatch, value='')
    sent = pagetests._capture_notifications(monkeypatch)
    calls, fetch = _recording_fetch()
    monkeypatch.setattr(dlg, 'fetch_label_growth_inputs', fetch)
    root = pagetests._draw(nicegui_client, account_id, _two_labels(account_id))
    _click(nicegui_client, pagetests._marked(root, page.MARKER_LABEL_REBAL_TEST)[0])
    assert calls == []
    assert not _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_DIALOG)
    assert any('FMP_API_KEY' in m and t == 'warning' for m, t in sent)


def test_opening_and_using_the_tester_writes_nothing(monkeypatch, nicegui_client, account_id, test_engine):
    """Read-only, PROVEN like the growth simulator's: every statement while the icon is clicked, the dialog loads,
    the scope and the parameters change is a SELECT, and no table changes size."""
    from sqlalchemy import event, func, select
    from sqlmodel import Session, SQLModel

    set_managed_label(account_id, 'ARK26', target_pct=60.0)
    pagetests._fmp_key(monkeypatch)
    calls, fetch = _recording_fetch()
    monkeypatch.setattr(dlg, 'fetch_label_growth_inputs', fetch)
    root = pagetests._draw(nicegui_client, account_id, _two_labels(account_id))

    def counts():
        with Session(test_engine) as session:
            return {t.name: session.exec(select(func.count()).select_from(t)).one()[0]
                    for t in SQLModel.metadata.sorted_tables}

    before = counts()
    writes = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ''
        if verb not in ('SELECT', 'PRAGMA', 'BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'):
            writes.append(statement)

    event.listen(test_engine, 'before_cursor_execute', spy)
    try:
        _click(nicegui_client, pagetests._marked(root, page.MARKER_LABEL_REBAL_TEST)[0])
        cost = _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_COST)[0]
        scope = _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_SCOPE)[0]
        with nicegui_client:
            pagetests._drive_value(cost, 25.0)
        _settle_in(nicegui_client, lambda: pagetests._drive_value(scope, dlg.ACCOUNT_SCOPE))
    finally:
        event.remove(test_engine, 'before_cursor_execute', spy)
    assert writes == []
    assert counts() == before


# ---------------------------------------------------------------------------
# the dialog
# ---------------------------------------------------------------------------

SHARES = {'ARK26': {'AAPL': 50.0, 'MSFT': 50.0}, 'Income': {'JEPI': 75.0, 'MSFT': 25.0}}
TARGETS = {'ARK26': 60.0, 'Income': 40.0}


def test_switching_scope_fetches_only_what_is_missing_and_params_never_refetch(nicegui_client):
    calls, fetch = _recording_fetch()
    d = _open(nicegui_client, SHARES, TARGETS, 'ARK26', fetch)
    assert calls == [['AAPL', 'MSFT']]
    first = d.result
    with nicegui_client:
        pagetests._drive_value(d.cost_input, 100.0)
        pagetests._drive_value(d.k_win_input, 0.5)
        pagetests._drive_value(d.lookback_toggle, 3)
    assert calls == [['AAPL', 'MSFT']]
    assert d.params == sim.RebalanceParams(lookback_years=3, cost_bps=100.0, k_winners=0.5, k_losers=0.0)
    assert d.result is not first and d.result.window.lookback_start == '2023-11-12'
    _settle_in(nicegui_client, lambda: pagetests._drive_value(d.scope_select, dlg.ACCOUNT_SCOPE))
    assert calls == [['AAPL', 'MSFT'], ['JEPI']]
    assert sorted(d.result.window.members) == ['AAPL', 'JEPI', 'MSFT']
    _settle_in(nicegui_client, lambda: pagetests._drive_value(d.scope_select, 'Income'))
    assert calls == [['AAPL', 'MSFT'], ['JEPI']]                            # all already loaded
    assert sorted(d.result.window.members) == ['JEPI', 'MSFT']


def test_a_refusal_is_drawn_with_the_window_facts_not_numbers(nicegui_client):
    closes = dict(CLOSES, NEW={d.date().isoformat(): 10.0 for d in DAYS if d >= pd.Timestamp('2026-06-01')})
    _calls, fetch = _recording_fetch(closes)
    d = _open(nicegui_client, {'L': {'AAPL': 70.0, 'NEW': 30.0}}, {'L': 50.0}, 'L', fetch)
    assert d.result.refusal and 'at least 2' in d.result.refusal
    assert _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_REFUSAL)
    assert not _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_TABLE)
    texts = _texts(nicegui_client)
    assert any('NEW (first price 2026-06-01)' in t for t in texts)
    assert any('1 of 2 member(s)' in t for t in texts)


def test_a_provider_error_is_shown_in_the_dialog(nicegui_client, monkeypatch):
    errors = []
    monkeypatch.setattr(dlg.logger, 'error', lambda msg, *a, **k: errors.append(str(msg)))

    def fetch(*a, **k):
        raise PriceSourceUnreachable('The price source is busy: every download slot is in use', {'busy': True})

    d = _open(nicegui_client, SHARES, TARGETS, 'ARK26', fetch)
    assert _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_ERROR)
    assert any('every download slot is in use' in t for t in _texts(nicegui_client))
    assert errors and d.result is None


def test_a_bad_parameter_is_shown_and_a_good_one_recovers(nicegui_client):
    _calls, fetch = _recording_fetch()
    d = _open(nicegui_client, SHARES, TARGETS, 'ARK26', fetch)
    with nicegui_client:
        pagetests._drive_value(d.cost_input, None)
    assert _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_ERROR) and d.result is None
    with nicegui_client:
        pagetests._drive_value(d.cost_input, 10.0)
    assert d.result is not None and d.result.refusal is None


def test_a_failure_while_drawing_is_shown_not_left_on_the_spinner(nicegui_client, monkeypatch):
    monkeypatch.setattr(dlg.logger, 'error', lambda msg, *a, **k: None)
    monkeypatch.setattr(dlg.sim, 'chart_options', lambda r: (_ for _ in ()).throw(RuntimeError('chart broke')))
    d = _open(nicegui_client, SHARES, TARGETS, 'ARK26', _recording_fetch()[1])
    assert _marked_anywhere(nicegui_client, dlg.MARKER_REBAL_ERROR)
    assert any('chart broke' in t for t in _texts(nicegui_client))
    assert not any('Loading' in t for t in _texts(nicegui_client))
    assert d.load_task.done() and d.load_task.exception() is None


def test_a_dialog_closed_during_the_fetch_finishes_quietly(nicegui_client, monkeypatch):
    errors = []
    monkeypatch.setattr(dlg.logger, 'error', lambda msg, *a, **k: errors.append(str(msg)))
    holder = {}

    def fetch(account_id, symbols, *, as_of, api_key):
        holder['d'].dialog.delete()
        return _inputs_for(symbols)

    async def _run():
        with nicegui_client:
            holder['d'] = dlg.open_rebalance_tester(1, SHARES, TARGETS, scope='ARK26', api_key='K', as_of=AS_OF,
                                                    fetch=fetch)
            await asyncio.gather(holder['d'].load_task, return_exceptions=True)
    asyncio.run(_run())
    assert holder['d'].load_task.exception() is None and holder['d'].result is None and errors == []


def test_closing_the_dialog_deletes_it(nicegui_client):
    d = _open(nicegui_client, SHARES, TARGETS, 'ARK26', _recording_fetch()[1])
    with nicegui_client:
        d.close()
    assert d.dialog.is_deleted


def test_the_page_inputs_are_the_live_effective_splits_and_targets():
    from types import SimpleNamespace
    live = page._new_live_state(exclusions={'TSLA': object()})
    for label, target, rows, weights in (('A', 60.0, ('AAPL', 'TSLA'), {'AAPL': 50.0, 'TSLA': 50.0}),
                                         ('B', 40.0, ('JEPI', 'NVDA'), {'JEPI': 100.0})):
        view = SimpleNamespace(label=label, target_pct=target, rows=[SimpleNamespace(symbol=s) for s in rows])
        live['views'].append(view)
        live['view_by_label'][label] = view
        live['weights'][label] = weights
    shares, targets, outside = page.rebalance_tester_inputs(live)
    assert shares == {'A': pytest.approx({'AAPL': 100.0}), 'B': {'JEPI': 100.0}}
    assert targets == {'A': 60.0, 'B': 40.0}
    assert 'TSLA (excluded' in outside['A'] and 'NVDA (no share set)' in outside['B']
