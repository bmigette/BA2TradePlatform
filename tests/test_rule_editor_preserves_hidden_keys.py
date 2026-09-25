"""The rule editor must not strip action keys it has no widget for.

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
must stay cleared, and it would be resurrected by a blanket carry-over. When the type
changes, a carried key no longer means anything on the new action -- it is dropped, and the
user is TOLD, rather than having it vanish.

Driven through the real editor (the ``_Editor`` harness from
``test_option_ui_param_reachability``): real widgets, the row's own change handler, the real
``_save_rule``, assertions on the dict handed to ``update_instance``.
"""
import pytest

from ba2_common.core.types import ExpertActionType, get_arc_floor_action_values

from tests.test_option_ui_param_reachability import _Editor

CLOSE = ExpertActionType.CLOSE_OPTION.value
BUY_CALL = ExpertActionType.BUY_CALL.value
BUY = ExpertActionType.BUY.value
IRON_CONDOR = ExpertActionType.OPEN_IRON_CONDOR.value


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
    """Every ``ui.notify`` the save path raises, as (message, type)."""
    seen = []
    monkeypatch.setattr(settings_module.ui, 'notify',
                        lambda msg, *a, **k: seen.append((str(msg), k.get('type'))))
    return seen


@pytest.fixture
def editor(settings_module, nicegui_client, monkeypatch, notices):
    with nicegui_client:
        yield _Editor(settings_module, monkeypatch)


def _drop_notices(notices):
    return [(m, t) for m, t in notices if 'dropped' in m.lower()]


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


def test_a_legacy_type_key_counts_as_the_same_type(editor):
    """Old rows store the type under ``type`` rather than ``action_type``; the editor loads
    either, so the carry-over must treat either as "unchanged"."""
    editor.tab._add_action_row(action_key='a0',
                               action_config={'type': CLOSE, 'close_target': 'covered_call'})
    assert editor.save().get('close_target') == 'covered_call'


# --------------------------------------------------------------------------- #
# changed type: dropped, and the user is told
# --------------------------------------------------------------------------- #
def test_changing_the_type_drops_close_target_with_a_notice(editor, notices):
    saved = editor.add_row(CLOSE, close_target='covered_call').choose(BUY_CALL).save()
    assert 'close_target' not in saved, saved
    drops = _drop_notices(notices)
    assert len(drops) == 1 and 'close_target' in drops[0][0], notices
    assert drops[0][1] == 'warning'


def test_changing_the_type_drops_entry_keys_with_a_notice(editor, notices):
    saved = editor.add_row(BUY_CALL, entry_cross=0.5, entry_tag=1).choose(IRON_CONDOR).save()
    assert 'entry_cross' not in saved and 'entry_tag' not in saved, saved
    drops = _drop_notices(notices)
    assert len(drops) == 1, notices
    assert 'entry_cross' in drops[0][0] and 'entry_tag' in drops[0][0]


def test_no_notice_when_nothing_hidden_was_dropped(editor, notices):
    editor.add_row(BUY_CALL).choose(IRON_CONDOR).save()
    assert _drop_notices(notices) == []


# --------------------------------------------------------------------------- #
# the carry-over is NAMED: a widget field the user cleared stays cleared
# --------------------------------------------------------------------------- #
def test_a_cleared_min_arc_is_not_resurrected(editor):
    credit = sorted(get_arc_floor_action_values())[0]
    editor.add_row(credit, min_arc=0.15)
    assert editor.widget('min_arc_input').value == pytest.approx(15.0)
    editor.widget('min_arc_input').value = None
    saved = editor.save()
    assert 'min_arc' not in saved, f"a cleared ARC floor came back: {saved}"


def test_a_cleared_strike_param_is_not_resurrected(editor):
    editor.add_row(BUY_CALL, strike_param=0.30, entry_cross=0.75)
    editor.widget('strike_param_input').value = ''
    saved = editor.save()
    assert 'strike_param' not in saved, saved
    assert saved['entry_cross'] == 0.75


def test_unlisted_unknown_keys_are_not_carried(editor):
    """Named list, not a blanket copy: a key nobody listed is still dropped."""
    saved = editor.add_row(BUY_CALL, some_stale_key=1).save()
    assert 'some_stale_key' not in saved


def test_every_preserved_key_is_widget_less(settings_module, nicegui_client, monkeypatch,
                                            notices):
    """A widget-backed key in the carry list would override (or resurrect) what the user
    typed. Build a fresh row of every action type and save it: no preserved key may ever
    come out of the WIDGETS."""
    preserved = settings_module.PRESERVED_HIDDEN_ACTION_KEYS
    assert {'close_target', 'entry_cross', 'entry_tag'} <= set(preserved)
    for action_type in ExpertActionType:
        with nicegui_client:
            saved = _Editor(settings_module, monkeypatch).add_row(action_type.value).save()
        leaked = set(preserved) & set(saved)
        assert not leaked, f"{action_type.value}: widget wrote preserved key(s) {leaked}"
