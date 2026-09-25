"""The rule editor must not strip action keys it has no widget for -- or drop anything silently.

WHY (2026-09-25): ``_save_rule`` rebuilds every action's ``action_config`` from the editor's
widgets alone. A key the editor never renders was therefore silently DROPPED the moment a
user opened a rule and pressed Save -- with nothing on screen to say so:

  * ``close_target`` -- the covered-call / wheel close (``cc_dte``: ``close_option`` with
    ``close_target='covered_call'``). CLOSE_OPTION renders no widgets at all, so a re-saved
    live rule lost its target and went back to closing "the evaluated order", which on those
    equity-entry overlays is the STOCK position, not the call.
  * ``entry_cross`` -- the option entry's limit-placement concession (deliberately no widget).
  * ``entry_tag`` -- the stage-2 style-group member label (arrives with plan/option-stage2).
  * ``lot_size`` -- the round-lot constraint on BUY for the option overlays; BUY renders no
    widgets, so the same strip applied.

The fix carries a NAMED list of those keys forward from the loaded config when the action
type is unchanged. Named, not "every unknown key": a widget-backed field the user cleared
must stay cleared, and it would be resurrected by a blanket carry-over. Every OTHER loaded key
that no rendered widget could have cleared and that is not carried (``min_volume``,
``take_profit_price``, a share action's ``value``, anything after a type change) is still
dropped -- but with a warning, shown only once the save has actually been written.

Same failure class, fixed alongside: new rows were keyed ``action_{len(rows)}``, so deleting
a row and adding one could reuse a LIVE row's id and overwrite it on save.

Driven through the real editor (the ``_Editor`` harness from
``test_option_ui_param_reachability``): real widgets, the row's own change handler, the real
``_save_rule``, assertions on the dict handed to ``update_instance``.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.option_selection_policy import WIRED_WEIGHT_BANDS
from ba2_common.core.types import ExpertActionType, get_arc_floor_action_values

from tests.test_option_ui_param_reachability import _Editor

CLOSE = ExpertActionType.CLOSE_OPTION.value
BUY_CALL = ExpertActionType.BUY_CALL.value
BUY = ExpertActionType.BUY.value
IRON_CONDOR = ExpertActionType.OPEN_IRON_CONDOR.value
ADJUST_TP = ExpertActionType.ADJUST_TAKE_PROFIT.value
INCREASE_SHARE = ExpertActionType.INCREASE_INSTRUMENT_SHARE.value
SAVED_OK = 'Rule saved successfully!'


@pytest.fixture(scope="module")
def settings_module():
    from ba2_trade_platform.ui.pages import settings

    return settings


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page

    client = Client(nicegui_page('/test-rule-editor-hidden-keys'), request=None)
    yield client
    client.remove_elements(client.elements.values())


@pytest.fixture
def notices(settings_module, monkeypatch):
    """Every ``ui.notify`` the save path raises, as (message, type), in order."""
    seen = []
    monkeypatch.setattr(settings_module.ui, 'notify',
                        lambda msg, *a, **k: seen.append((str(msg), k.get('type'))))
    return seen


@pytest.fixture
def editor(settings_module, nicegui_client, monkeypatch, notices):
    with nicegui_client:
        yield _Editor(settings_module, monkeypatch)


def _drop_notices(notices):
    return [(m, t) for m, t in notices if 'dropped' in m]


def _save_all(editor):
    """``_save_rule`` for a multi-row rule: the whole persisted actions dict."""
    rule = SimpleNamespace(id=1, name=None, type=None, subtype=None, triggers=None,
                           actions=None, continue_processing=None)
    editor.tab._save_rule(rule)
    assert editor.errors == [], f"_save_rule swallowed an exception: {editor.errors}"
    assert len(editor.saved) == 1, "the rule was never handed to update_instance"
    return editor.saved[0]


# --------------------------------------------------------------------------- #
# unchanged type: the hidden keys survive an edit-and-save
# --------------------------------------------------------------------------- #
def test_close_target_survives_a_resave_of_a_covered_call_close(editor, notices):
    """The live bug: the O_CC / O_WHEEL ``cc_dte`` close lost its target on any re-save."""
    saved = editor.add_row(CLOSE, close_target='covered_call').save()
    assert saved.get('close_target') == 'covered_call', (
        f"re-saving the covered-call close stripped its target: {saved}")
    assert _drop_notices(notices) == []


def test_entry_cross_and_entry_tag_survive_a_resave_of_an_option_entry(editor, notices):
    saved = editor.add_row(BUY_CALL, strike_method='delta', strike_param=0.30,
                           entry_cross=0.75, entry_tag=2).save()
    assert saved.get('entry_cross') == 0.75, f"entry_cross was stripped: {saved}"
    assert saved.get('entry_tag') == 2, f"entry_tag was stripped: {saved}"
    # ...and the widget-backed fields still come from the widgets.
    assert saved['strike_param'] == 0.30
    assert _drop_notices(notices) == []


def test_lot_size_survives_a_resave_of_a_buy(editor, notices):
    """BUY renders no widgets, so its round-lot constraint was stripped the same way."""
    saved = editor.add_row(BUY, lot_size=100).save()
    assert saved.get('lot_size') == 100, f"lot_size was stripped: {saved}"
    assert _drop_notices(notices) == []


def test_a_legacy_type_key_counts_as_the_same_type(editor, notices):
    """Old rows store the type under ``type`` rather than ``action_type``; the editor loads
    either, so the carry-over must treat either as "unchanged" -- and rewriting ``type`` as
    ``action_type`` is not a drop."""
    editor.tab._add_action_row(action_key='a0',
                               action_config={'type': CLOSE, 'close_target': 'covered_call'})
    assert editor.save().get('close_target') == 'covered_call'
    assert _drop_notices(notices) == []


# --------------------------------------------------------------------------- #
# changed type: dropped, and the user is told
# --------------------------------------------------------------------------- #
def test_changing_the_type_drops_close_target_with_a_notice(editor, notices):
    saved = editor.add_row(CLOSE, close_target='covered_call').choose(BUY_CALL).save()
    assert 'close_target' not in saved, saved
    drops = _drop_notices(notices)
    assert len(drops) == 1 and 'close_target' in drops[0][0], notices
    assert 're-author it for the new type' in drops[0][0]
    assert drops[0][1] == 'warning'


def test_changing_the_type_drops_entry_keys_with_a_notice(editor, notices):
    saved = editor.add_row(BUY_CALL, entry_cross=0.5, entry_tag=1).choose(IRON_CONDOR).save()
    assert 'entry_cross' not in saved and 'entry_tag' not in saved, saved
    drops = _drop_notices(notices)
    assert len(drops) == 1, notices
    assert 'entry_cross' in drops[0][0] and 'entry_tag' in drops[0][0]


def test_changing_the_type_names_a_widget_key_the_new_type_has_no_field_for(editor, notices):
    """wing_width_pct had a field on the condor and has none on a long call: gone, and said."""
    saved = editor.add_row(IRON_CONDOR, wing_width_pct=7.5).choose(BUY_CALL).save()
    assert 'wing_width_pct' not in saved
    drops = _drop_notices(notices)
    assert len(drops) == 1 and 'wing_width_pct' in drops[0][0], notices


def test_no_notice_when_nothing_was_dropped(editor, notices):
    editor.add_row(BUY_CALL).choose(IRON_CONDOR).save()
    assert _drop_notices(notices) == []


# --------------------------------------------------------------------------- #
# unchanged type, key neither widget-backed nor carried: dropped VISIBLY
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('action_type,config,key', [
    (BUY_CALL, {'min_volume': 50}, 'min_volume'),
    (ADJUST_TP, {'value': 5.0, 'reference_value': 'order_open_price',
                 'take_profit_price': 123.0}, 'take_profit_price'),
    (INCREASE_SHARE, {'target_percent': 10.0, 'value': 10.0}, 'value'),
    (BUY_CALL, {'some_stale_key': 1}, 'some_stale_key'),
])
def test_an_uncarried_key_without_a_field_is_dropped_with_a_notice(editor, notices,
                                                                   action_type, config, key):
    saved = editor.add_row(action_type, **config).save()
    assert key not in saved, f"{key} must not be carried: {saved}"
    drops = _drop_notices(notices)
    assert len(drops) == 1, notices
    assert key in drops[0][0] and '(no editor field)' in drops[0][0]
    assert drops[0][1] == 'warning'


def test_drop_notices_are_shown_only_after_the_save_is_written(editor, notices):
    """Shown AFTER the success notice -- never before the write."""
    editor.add_row(BUY_CALL, min_volume=50).save()
    messages = [m for m, _ in notices]
    assert SAVED_OK in messages
    drop_at = next(i for i, m in enumerate(messages) if 'dropped' in m)
    assert drop_at > messages.index(SAVED_OK), messages


def test_a_refused_save_shows_no_drop_notice(editor, notices):
    """Row a0 is collected (and would warn); row a1 then refuses the save. Nothing was
    written, so nothing was dropped -- a notice would describe a save that did not happen."""
    editor.add_row(BUY_CALL, min_volume=50)
    editor.tab._add_action_row(action_key='a1',
                               action_config={'action_type': INCREASE_SHARE,
                                              'target_percent': 10.0})
    editor.tab.actions['a1']['target_percent_input']().value = 150.0
    editor.tab._save_rule(SimpleNamespace(id=1, name=None, type=None, subtype=None,
                                          triggers=None, actions=None,
                                          continue_processing=None))
    assert editor.saved == [], "the refused rule was written"
    assert any(t == 'negative' for _, t in notices), notices
    assert _drop_notices(notices) == [], notices


# --------------------------------------------------------------------------- #
# the carry-over is NAMED: a widget field the user cleared stays cleared, silently
# --------------------------------------------------------------------------- #
def test_a_cleared_min_arc_is_not_resurrected(editor, notices):
    credit = sorted(get_arc_floor_action_values())[0]
    editor.add_row(credit, min_arc=0.15)
    assert editor.widget('min_arc_input').value == pytest.approx(15.0)
    editor.widget('min_arc_input').value = None
    saved = editor.save()
    assert 'min_arc' not in saved, f"a cleared ARC floor came back: {saved}"
    assert _drop_notices(notices) == [], "clearing a field is the user's choice, not a drop"


def test_a_cleared_strike_param_is_not_resurrected(editor, notices):
    editor.add_row(BUY_CALL, strike_param=0.30, entry_cross=0.75)
    editor.widget('strike_param_input').value = ''
    saved = editor.save()
    assert 'strike_param' not in saved, saved
    assert saved['entry_cross'] == 0.75
    assert _drop_notices(notices) == []


# --------------------------------------------------------------------------- #
# row ids: a new row never takes a live row's id
# --------------------------------------------------------------------------- #
def test_each_row_of_a_multi_action_rule_keeps_its_own_hidden_key(editor):
    editor.tab._add_action_row('action_0', {'action_type': BUY, 'lot_size': 100})
    editor.tab._add_action_row('action_1', {'action_type': CLOSE,
                                            'close_target': 'covered_call'})
    saved = _save_all(editor)
    assert saved['action_0'] == {'action_type': BUY, 'lot_size': 100}
    assert saved['action_1'] == {'action_type': CLOSE, 'close_target': 'covered_call'}


def test_remove_then_add_keeps_every_remaining_rows_config(editor):
    """Load action_0 + action_1, delete action_0, Add: the new row used to be keyed
    ``action_{len}`` == action_1, replacing the covered-call close's refs while its card
    stayed on screen -- and the save wrote ``{'action_1': {'action_type': 'buy'}}``."""
    tab = editor.tab
    tab._add_action_row('action_0', {'action_type': BUY, 'lot_size': 100})
    tab._add_action_row('action_1', {'action_type': CLOSE, 'close_target': 'covered_call'})
    card_1 = tab.actions['action_1']['card']
    tab._remove_action_row('action_0', tab.actions['action_0']['card'])
    tab._add_action_row()
    assert len(tab.actions) == 2, sorted(tab.actions)
    assert tab.actions['action_1']['card'] is card_1, "the new row took action_1's id"
    saved = _save_all(editor)
    assert saved['action_1'] == {'action_type': CLOSE, 'close_target': 'covered_call'}
    (new_id,) = set(saved) - {'action_1'}
    assert saved[new_id] == {'action_type': BUY}


def test_remove_then_add_does_not_reuse_a_live_trigger_id(editor):
    from nicegui import ui

    tab = editor.tab
    tab.triggers_container = ui.column()
    tab._add_trigger_row('trigger_0', None)
    tab._add_trigger_row('trigger_1', None)
    card_1 = tab.triggers['trigger_1']['card']
    tab._remove_trigger_row('trigger_0', tab.triggers['trigger_0']['card'])
    tab._add_trigger_row()
    assert len(tab.triggers) == 2, sorted(tab.triggers)
    assert tab.triggers['trigger_1']['card'] is card_1


def test_first_unused_row_id(settings_module):
    f = settings_module._first_unused_row_id
    assert f('action', {}) == 'action_0'
    assert f('action', {'action_1': 1}) == 'action_2'
    assert f('action', {'a0': 1, 'a1': 1}) == 'action_2'


# --------------------------------------------------------------------------- #
# the key tables stay honest
# --------------------------------------------------------------------------- #
def test_the_widget_key_table_covers_every_widget_ref_on_a_row(editor, settings_module):
    """A widget ref missing from ACTION_WIDGET_KEYS would make its cleared field read as a
    silent 'no editor field' drop; a stale entry would KeyError the save."""
    editor.add_row(BUY_CALL)
    refs = set(editor.tab.actions['a0']) - {'card', 'type_select', 'loaded_config'}
    assert refs == {ref for ref, _ in settings_module.ACTION_WIDGET_KEYS}


def _fill_every_widget(refs, widget_keys):
    """Give every RENDERED widget a value the save will write, so a widget that writes a
    preserved key cannot hide behind an empty default."""
    rendered = {}
    for ref, key in widget_keys:
        w = refs[ref]()
        if w is None:
            continue
        rendered[key] = w
        if key in WIRED_WEIGHT_BANDS:
            lo, hi = WIRED_WEIGHT_BANDS[key]
            w.value = (lo + hi) / 2 or 0.5
        elif key == 'value':
            w.value = '5'
        elif key == 'strike_param':
            w.value = '0.3'
        elif key == 'min_one_contract':
            w.value = True
        elif w.value is None or w.value == '':
            w.value = 10.0
    return rendered


def test_no_widget_writes_a_preserved_key(settings_module, nicegui_client, monkeypatch,
                                          notices):
    """A widget-backed key in the carry list would override (or resurrect) what the user
    typed. For every action type: fill EVERY rendered widget, save, and check both that each
    filled widget really wrote its key and that no preserved key came out of the widgets."""
    preserved = set(settings_module.PRESERVED_HIDDEN_ACTION_KEYS)
    assert {'close_target', 'entry_cross', 'entry_tag'} <= preserved
    widget_keys = {key for _, key in settings_module.ACTION_WIDGET_KEYS}
    assert not preserved & widget_keys, preserved & widget_keys
    for action_type in ExpertActionType:
        with nicegui_client:
            ed = _Editor(settings_module, monkeypatch).add_row(action_type.value)
            rendered = _fill_every_widget(ed.tab.actions['a0'],
                                          settings_module.ACTION_WIDGET_KEYS)
            saved = ed.save()
        missing = set(rendered) - set(saved)
        assert not missing, f"{action_type.value}: filled widgets wrote nothing for {missing}"
        leaked = preserved & set(saved)
        assert not leaked, f"{action_type.value}: widget wrote preserved key(s) {leaked}"
