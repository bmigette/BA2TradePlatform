"""Overview page-level label scope + DB-persisted chart selections.

Pure rules are tested directly; persistence round-trips through the real
AppSetting table (the conftest autouse fixtures point the engine at an in-memory
DB). The UI wiring cannot run without a browser, so it is guarded by source
checks and by building the real AccountGrowthTab pure helpers.
"""
from pathlib import Path

import pytest

from ba2_trade_platform.ui.utils import overview_label_scope as ols
from ba2_trade_platform.ui.utils.overview_label_scope import (
    apply_scope, chart_key, default_scope, follow_pf_key, pick_single,
    read_follow_pf, read_overview_setting, read_stored_list,
    resolve_chart_selection, resolve_scope, scope_key, select_all, select_none,
    single_key, union_scopes, write_overview_setting,
)

OVERVIEW_PY = (Path(__file__).resolve().parents[1]
               / 'ba2_trade_platform' / 'ui' / 'pages' / 'overview.py')


# ---- scope resolution -------------------------------------------------------

def test_default_scope_is_every_traded_label_including_auto_added():
    assert resolve_scope(None, ['ARK26', 'auto_added', 'ARK26', 'Unlabeled']) == \
        ['ARK26', 'Unlabeled', 'auto_added']


def test_stored_scope_wins_over_default():
    assert resolve_scope(['B'], ['A', 'B', 'C']) == ['B']


def test_empty_stored_scope_is_respected():
    assert resolve_scope([], ['A', 'B']) == []


def test_stored_scope_may_hold_a_label_that_no_longer_exists():
    """Deleted labels are harmless: scope only intersects chart options."""
    assert apply_scope(['A', 'B'], resolve_scope(['A', 'GONE'], ['A', 'B'])) == ['A']


def test_follow_pf_uses_managed_labels_over_stored_and_default():
    assert resolve_scope(['X'], ['A', 'B'], follow_pf=True, managed=['M2', 'M1']) == ['M1', 'M2']


def test_follow_pf_with_no_managed_labels_falls_back():
    assert resolve_scope(['X'], ['A'], follow_pf=True, managed=[]) == ['X']
    assert resolve_scope(None, ['A'], follow_pf=True, managed=[]) == ['A']


def test_managed_labels_ignored_when_not_following():
    assert resolve_scope(None, ['A'], follow_pf=False, managed=['M']) == ['A']


def test_union_across_accounts_with_different_selections():
    acc1 = resolve_scope(['A'], ['A', 'B'])            # saved
    acc2 = resolve_scope(None, ['B', 'C'])             # default
    acc3 = resolve_scope(None, ['Z'], True, ['M'])     # follows pf
    assert union_scopes([acc1, acc2, acc3]) == ['A', 'B', 'C', 'M']


def test_union_of_nothing_is_empty():
    assert union_scopes([]) == []
    assert union_scopes([[], []]) == []


# ---- scope applied to charts --------------------------------------------------

def test_apply_scope_keeps_label_order_and_none_means_unrestricted():
    assert apply_scope(['C', 'A', 'B'], ['A', 'C']) == ['C', 'A']
    assert apply_scope(['C', 'A'], None) == ['C', 'A']
    assert apply_scope(['C', 'A'], []) == []


def test_chart_selection_never_saved_is_todays_default():
    assert resolve_chart_selection([None], ['auto_added', 'A', 'B']) == ['A', 'B']


def test_chart_selection_empty_is_respected_and_deleted_dropped():
    assert resolve_chart_selection([[]], ['A']) == []
    assert resolve_chart_selection([['A', 'GONE']], ['A', 'B']) == ['A']


def test_chart_selection_stored_outside_scope_is_cut_by_options():
    # options already carry the scope; a stored label out of scope vanishes
    assert resolve_chart_selection([['A', 'B']], apply_scope(['A', 'B', 'C'], ['B', 'C'])) == ['B']


def test_chart_selection_unions_accounts_in_options_order():
    opts = ['A', 'B', 'C', 'D']
    assert resolve_chart_selection([['C'], ['A'], []], opts) == ['A', 'C']
    # an account that never saved contributes its default (all but auto_added)
    assert resolve_chart_selection([['C'], None], ['auto_added', 'C', 'D']) == ['C', 'D']


def test_select_all_and_none():
    assert select_all(['A', 'B', 'A', '']) == ['A', 'B']
    assert select_none() == []


def test_pick_single_prefers_first_valid_stored_value():
    assert pick_single([None, 'GONE', 'B'], ['A', 'B'], 'A') == 'B'
    assert pick_single([None], ['A'], 'A') == 'A'
    assert pick_single([], ['A'], None) is None


def test_keys_are_per_account_and_per_purpose():
    assert scope_key(3) == 'overview_labels_scope_3'
    assert follow_pf_key(3) == 'overview_labels_scope_follow_pf_3'
    assert chart_key('growth', 3) == 'overview_growth_labels_3'
    assert single_key('position_symbol', 3) == 'overview_position_symbol_3'
    assert scope_key(3) != scope_key(4)


# ---- DB round trip ------------------------------------------------------------

def test_setting_round_trips_through_app_setting():
    assert read_overview_setting(scope_key(1)) is None
    assert write_overview_setting(scope_key(1), ['A', 'B'])
    assert read_overview_setting(scope_key(1)) == ['A', 'B']
    assert write_overview_setting(scope_key(1), [])           # update, not insert
    assert read_overview_setting(scope_key(1)) == []
    assert read_stored_list(scope_key(1)) == []


def test_accounts_are_stored_independently_and_follow_flag_round_trips():
    write_overview_setting(chart_key('growth', 1), ['A'])
    write_overview_setting(chart_key('growth', 2), ['B'])
    assert read_stored_list(chart_key('growth', 1)) == ['A']
    assert read_stored_list(chart_key('growth', 2)) == ['B']
    assert read_follow_pf(1) is False
    write_overview_setting(follow_pf_key(1), True)
    assert read_follow_pf(1) is True and read_follow_pf(2) is False


def test_legacy_browser_value_is_migrated_once(monkeypatch):
    from nicegui import app

    class _S:
        user = {'overview_growth_labels': ['LEG']}
    monkeypatch.setattr(app, 'storage', _S())
    key = chart_key('growth', 7)
    assert read_stored_list(key, 'overview_growth_labels') == ['LEG']
    assert read_overview_setting(key) == ['LEG']              # written through
    _S.user['overview_growth_labels'] = ['CHANGED']
    assert read_stored_list(key, 'overview_growth_labels') == ['LEG']   # DB now wins


def test_corrupt_stored_value_is_treated_as_absent():
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import AppSetting
    with get_db() as session:
        session.add(AppSetting(key=scope_key(9), value_str='{not json'))
        session.commit()
    assert read_overview_setting(scope_key(9)) is None
    write_overview_setting(scope_key(10), 'ARK26')           # not a list
    assert read_stored_list(scope_key(10)) is None


def test_scope_inputs_default_comes_from_traded_symbols_labels():
    """_compute_scope_inputs reuses get_labels_by_symbol on the account's trades,
    dividends and positions; unlabeled symbols count as 'Unlabeled'."""
    from types import SimpleNamespace
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import Instrument
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    add_instance(Instrument(name='AAPL', labels=['tech', 'auto_added']))
    add_instance(Instrument(name='XOM', labels=['energy']))
    tab = AccountGrowthTab.__new__(AccountGrowthTab)
    acc = SimpleNamespace(id=1, name='A1')
    state = tab._compute_scope_inputs(
        [(acc, None)],
        [{'symbol': 'AAPL', 'account_id': 1}, {'symbol': 'XOM', 'account_id': 2}],
        [{'symbol': 'NOPE', 'account_id': 1}],
        {1: {'ZZZ'}},
    )
    assert state[1]['traded'] == ['Unlabeled', 'auto_added', 'tech']
    assert state[1]['stored'] is None and state[1]['follow'] is False
    assert AccountGrowthTab._effective_scope(state) == ['Unlabeled', 'auto_added', 'tech']


def test_effective_scope_unions_accounts_and_follows_pf():
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    state = {
        1: {'traded': ['A', 'B'], 'managed': [], 'stored': ['A'], 'follow': False},
        2: {'traded': ['C'], 'managed': ['M'], 'stored': None, 'follow': True},
        3: {'traded': ['D'], 'managed': [], 'stored': None, 'follow': False},
    }
    assert AccountGrowthTab._effective_scope(state) == ['A', 'D', 'M']


def test_tab_scopes_symbol_info_and_selects_per_account():
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    tab = AccountGrowthTab.__new__(AccountGrowthTab)
    tab._scope = ['A']
    tab._account_ids = [1, 2]
    tab._single_account = None
    info = {'S1': {'qty': 1, 'labels': ['A', 'B']}, 'S2': {'qty': 1, 'labels': ['B']}}
    assert tab._scope_symbol_info(info) == {'S1': {'qty': 1, 'labels': ['A']}}
    tab._scope = None
    assert tab._scope_symbol_info(info) is info
    # All accounts: union of each account's saved selection; writes are not persisted
    write_overview_setting(chart_key('growth', 1), ['A'])
    write_overview_setting(chart_key('growth', 2), ['B'])
    assert tab._stored_selection('growth', 'overview_growth_labels', ['A', 'B', 'C']) == ['A', 'B']
    tab._persist_selection('growth', ['C'])
    assert read_stored_list(chart_key('growth', 1)) == ['A']
    tab._account_ids, tab._single_account = [2], 2
    assert tab._stored_selection('growth', 'overview_growth_labels', ['A', 'B', 'C']) == ['B']
    tab._persist_selection('growth', ['C'])
    assert read_stored_list(chart_key('growth', 2)) == ['C']
    assert read_stored_list(chart_key('growth', 1)) == ['A']


def test_overview_wires_scope_and_all_none_buttons():
    src = OVERVIEW_PY.read_text(encoding='utf-8')
    assert 'self._scope = self._effective_scope(' in src
    assert 'Follow portfolio manager labels' in src
    assert src.count('_multi_select_with_all_none(') >= 3      # def + 2 charts
    assert "_persist_single(CHART_POSITION_SYMBOL" in src


# ---- light UI construction (bare nicegui.Client, as other overview tests do) ----

@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-overview-label-scope'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _scope_ui(client, state, single):
    from nicegui import ui
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    tab = AccountGrowthTab.__new__(AccountGrowthTab)
    tab._account_ids = list(state)
    tab._single_account = single
    tab._scope = AccountGrowthTab._effective_scope(state)
    calls = []
    with client.content:
        holder = ui.column()
        tab._render_scope_controls(holder, state, lambda: calls.append(1))
    return tab, calls, client


def _find(client, cls):
    return [e for e in client.elements.values() if isinstance(e, cls)]


def test_scope_controls_edit_persist_and_follow_pf(nicegui_client, monkeypatch):
    from nicegui import ui
    from nicegui.events import ValueChangeEventArguments
    notes = []
    monkeypatch.setattr(ui, 'notify', lambda *a, **k: notes.append((a, k)))
    state = {5: {'name': 'A', 'traded': ['A', 'B'], 'managed': ['M'], 'stored': None, 'follow': False}}
    tab, calls, client = _scope_ui(nicegui_client, state, 5)
    select = [e for e in _find(client, ui.select)][0]
    switch = _find(client, ui.switch)[0]
    assert sorted(select.value) == ['A', 'B']

    select.value = ['A']                       # user edits -> persisted, charts redrawn
    assert read_overview_setting(scope_key(5)) == ['A'] and calls == [1]

    switch.value = True                        # follow: one-shot copy + persistent flag
    assert read_follow_pf(5) is True
    assert read_overview_setting(scope_key(5)) == ['M']
    assert state[5]['follow'] is True and sorted(select.value) == ['M']
    assert not select.enabled

    switch.value = False                       # off keeps the copied labels, unlocks
    assert read_follow_pf(5) is False and select.enabled
    assert read_overview_setting(scope_key(5)) == ['M']


def test_follow_pf_without_managed_labels_notifies_and_changes_nothing(nicegui_client, monkeypatch):
    from nicegui import ui
    notes = []
    monkeypatch.setattr(ui, 'notify', lambda *a, **k: notes.append((a, k)))
    state = {6: {'name': 'A', 'traded': ['A'], 'managed': [], 'stored': ['A'], 'follow': False}}
    _scope_ui(nicegui_client, state, 6)
    switch = _find(nicegui_client, ui.switch)[0]
    switch.value = True
    assert notes and 'no managed labels' in notes[0][0][0]
    assert switch.value is False and read_follow_pf(6) is False
    assert read_overview_setting(scope_key(6)) is None


def test_all_accounts_scope_is_read_only_union(nicegui_client):
    from nicegui import ui
    state = {1: {'name': 'A', 'traded': ['A'], 'managed': [], 'stored': None, 'follow': False},
             2: {'name': 'B', 'traded': ['B'], 'managed': [], 'stored': ['C'], 'follow': False}}
    _scope_ui(nicegui_client, state, None)
    assert _find(nicegui_client, ui.select) == [] and _find(nicegui_client, ui.switch) == []
    assert any(getattr(e, 'text', '') == 'A, C' for e in _find(nicegui_client, ui.label))


def test_multi_select_has_select_all_and_none_buttons(nicegui_client):
    from nicegui import ui
    from ba2_trade_platform.ui.pages.overview import AccountGrowthTab
    with nicegui_client.content:
        sel = AccountGrowthTab._multi_select_with_all_none(['A', 'B'], ['A'], 'Labels', 'w-64')
    buttons = {b.text: b for b in _find(nicegui_client, ui.button)}
    buttons['All'].on_click.__self__ if False else None
    assert set(buttons) == {'All', 'None'}
    sel.value = select_all(['A', 'B']); assert sel.value == ['A', 'B']
    sel.value = select_none(); assert sel.value == []
