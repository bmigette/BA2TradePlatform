"""The rule editor's categorised trigger picker, and the gate it still refuses.

Two halves of one change (docs/plans/2026-09-22-trigger-picker-design.md).

THE MENU. Eighty trigger types were one unsorted ``ui.select`` column, minus the
fifteen market-condition fields, which ``_authorable_trigger_types`` filtered out
entirely -- so an operator could not author a gate for an expert whose profile IS
set, could not read one a deployed expert was already running, and had no way to
learn the vocabulary existed. The filter is gone; a modal with seven categories,
counts, a search box and a per-entry "needs profile X" message replaces it.

THE GUARD. Removing a menu filter must not remove a refusal. A market gate on an
OPEN-POSITIONS rule is the worst outcome in this design -- outside the entry pass
the live resolver has no context, the condition reads ``no_context``, the rule
never fires, and the position's exit silently stops happening. That was refused
only when the rule was assembled INTO a ruleset; now that the fields are one click
away in the editor, the rule's own save refuses it too, in the same words.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import ba2_trade_platform.ui.pages.settings as settings_page
from ba2_trade_platform.ui.pages.settings import TradeSettingsTab, TriggerTypePicker

ADX = "underlying_adx_14"          # a market-condition field: needs a profile
STATE = "structure_state"          # the one CATEGORICAL trigger: a regime code, not a scale
CONFIDENCE = "confidence"          # an ordinary numeric trigger
HAS_POSITION = "has_position"      # an ordinary flag trigger


# --------------------------------------------------------------------------- scaffolding

@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw. No browser.

    Not decoration: NiceGUI resolves a widget's parent from a per-task slot stack and
    building one with that stack empty raises "The current slot cannot be determined".
    In the real editor the rule dialog supplies the slot.
    """
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-rule-trigger-picker'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _marked(root, marker):
    return [d for d in root.descendants(include_self=True)
            if marker in (getattr(d, '_markers', None) or ())]


def _marked_texts(root, marker):
    return [d.text for d in _marked(root, marker)]


def _click(element):
    """Fire an element's own click listener, with no browser and no event loop.

    The listener list is snapshotted because the handler REDRAWS the container the element
    sits in -- a chip click rebuilds the chip row -- and iterating a dict while the click
    deletes its owner raises instead of clicking.
    """
    fired = 0
    for listener in list(element._event_listeners.values()):
        if listener.type.split('.')[0] == 'click':
            listener.handler(None)
            fired += 1
    assert fired, f'no click handler on {element}'


def _picker(client, value=HAS_POSITION, on_change=None):
    with client:
        return TriggerTypePicker(value, on_change=on_change)


def _open(client, picker, category=None):
    """Open the modal the way the button's click does, and return the dialog.

    ``category`` presses one of the chips afterwards -- the modal opens on a category that
    HOLDS the current value, so a test that wants the whole catalog has to ask for All.
    """
    with client:
        _click(picker.button)
        if category is not None:
            _click(_chip(picker.dialog, category))
    return picker.dialog


def _entry_keys(dialog):
    return _marked_texts(dialog, TriggerTypePicker.MARKER_ENTRY_KEY)


def _entry_card(dialog, value):
    cards = _marked(dialog, f'{TriggerTypePicker.MARKER_ENTRY}-{value}')
    assert len(cards) == 1, f'{value}: {len(cards)} rows'
    return cards[0]


def _chip(dialog, category):
    chips = _marked(dialog, f'{TriggerTypePicker.MARKER_CHIP}-{category}')
    assert len(chips) == 1, f'{category}: {len(chips)} chips'
    return chips[0]


# =========================================================================================
# The picker
# =========================================================================================

def test_the_button_shows_the_friendly_name_over_the_raw_key(nicegui_client):
    """Both, not either. The friendly name is what a reader understands; the raw key is
    what is STORED and what every log, payload and backtest genome spells -- an operator
    comparing the editor to a deploy payload needs the two side by side."""
    from ba2_common.core.trigger_catalog import trigger_catalog

    known = {e.value: e for e in trigger_catalog()}
    picker = _picker(nicegui_client, HAS_POSITION)

    assert _marked_texts(picker.root, TriggerTypePicker.MARKER_VALUE_KEY) == [HAS_POSITION]
    assert _marked_texts(picker.root,
                         TriggerTypePicker.MARKER_VALUE_NAME) == [known[HAS_POSITION].name]


def test_a_value_the_catalog_does_not_know_renders_as_its_raw_key(nicegui_client):
    """THE FAILURE THIS PINS. NiceGUI refuses a select value outside its options
    (``choice_element``: ``ValueError: Invalid value: ...``) and ``show_rule_dialog`` wraps
    nothing, so a persisted trigger the menu did not offer raised mid-build and left a
    half-rendered dialog -- the rule became impossible even to LOOK at. ``_trigger_type_options``
    dodged that by appending the value to the options; a BUTTON has no options to be outside
    of, so the dodge is gone and the property is pinned directly."""
    picker = _picker(nicegui_client, 'a_trigger_from_a_future_release')

    assert _marked_texts(picker.root,
                         TriggerTypePicker.MARKER_VALUE_KEY) == ['a_trigger_from_a_future_release']
    assert _marked(picker.root, TriggerTypePicker.MARKER_UNKNOWN), (
        'an unrecognised key must SAY it is unrecognised, not pose as a working trigger')
    assert picker.value == 'a_trigger_from_a_future_release', 'and it must survive a re-save'


def test_a_null_trigger_type_still_renders(nicegui_client):
    """A malformed row (``event_type`` present but null) showed an empty select before and
    must not start crashing now -- the editor is the only door onto fixing such a row."""
    picker = _picker(nicegui_client, None)

    assert picker.value is None
    assert _marked(picker.root, TriggerTypePicker.MARKER_BUTTON)


def test_the_modal_lists_every_trigger_the_catalog_carries(nicegui_client):
    """The flat list's one virtue was that nothing could hide in it. A categorised menu
    must not introduce silent disappearance: All holds everything."""
    from ba2_common.core.trigger_catalog import trigger_catalog

    dialog = _open(nicegui_client, _picker(nicegui_client, HAS_POSITION), category='all')

    assert set(_entry_keys(dialog)) == {e.value for e in trigger_catalog()}


def test_the_market_condition_fields_are_offered_like_any_other_trigger(nicegui_client):
    """The regression the whole change exists for: ``_authorable_trigger_types`` filtered
    all fifteen out, which removed the feature from the UI rather than guarding it."""
    from ba2_common.core.market_condition_rules import market_condition_fields

    dialog = _open(nicegui_client, _picker(nicegui_client, HAS_POSITION), category='all')

    assert market_condition_fields() <= set(_entry_keys(dialog))


def test_a_market_field_entry_names_the_profile_the_expert_needs(nicegui_client):
    """The picker replaces a filter with a WARNING, and the warning has to be actionable:
    a gate with no profile behind it reads ``no_context`` and never passes, so the message
    names the setting that makes it work rather than saying "be careful"."""
    from ba2_common.core.trigger_catalog import trigger_catalog

    profile = {e.value: e.requires_profile for e in trigger_catalog()}[ADX]
    assert profile, 'a market field with no profile would make this test vacuous'

    dialog = _open(nicegui_client, _picker(nicegui_client, HAS_POSITION), category='all')
    note = _marked_texts(_entry_card(dialog, ADX), TriggerTypePicker.MARKER_ENTRY_PROFILE)

    assert len(note) == 1 and profile in note[0]
    # ... and an ordinary trigger carries no such note: a warning on everything is no warning.
    assert not _marked(_entry_card(dialog, CONFIDENCE), TriggerTypePicker.MARKER_ENTRY_PROFILE)


def test_every_category_chip_carries_its_count(nicegui_client):
    """The count is the reason a chip is a chip and not a tab: it tells the operator whether
    the category is worth opening before it is opened."""
    from ba2_common.core.trigger_catalog import CATEGORIES, triggers_in_category

    dialog = _open(nicegui_client, _picker(nicegui_client, HAS_POSITION))

    for category in CATEGORIES:
        label = _chip(dialog, category).text
        assert str(len(triggers_in_category(category))) in label, label
        assert category.title() in label, label


def test_a_category_chip_narrows_the_list_to_that_category(nicegui_client):
    from ba2_common.core.trigger_catalog import triggers_in_category

    dialog = _open(nicegui_client, _picker(nicegui_client, HAS_POSITION))
    _click(_chip(dialog, 'market'))

    assert set(_entry_keys(dialog)) == {e.value for e in triggers_in_category('market')}
    assert ADX in _entry_keys(dialog) and CONFIDENCE not in _entry_keys(dialog)


def test_the_search_box_filters_within_the_chosen_category(nicegui_client):
    """Search and category compose. Searching inside Market and getting a Position hit
    would mean the chip the operator just pressed had been silently dropped."""
    from ba2_common.core.trigger_catalog import search_triggers

    picker = _picker(nicegui_client, HAS_POSITION)
    dialog = _open(nicegui_client, picker)
    _click(_chip(dialog, 'market'))
    with nicegui_client:
        picker.search.value = 'adx'

    assert set(_entry_keys(dialog)) == {e.value for e in search_triggers('adx', 'market')}
    assert ADX in _entry_keys(dialog)


def test_the_search_box_takes_focus_on_open(nicegui_client):
    """Typing is the fastest route through eighty entries, and a search box you must click
    first is one the operator does not discover."""
    picker = _picker(nicegui_client, HAS_POSITION)
    _open(nicegui_client, picker)

    assert picker.search._props.get('autofocus')


def test_the_modal_opens_on_a_category_that_holds_the_current_value(nicegui_client):
    """Opened from an existing trigger, the picker starts where that trigger LIVES, so the
    neighbours it lists are the alternatives to it."""
    from ba2_common.core.trigger_catalog import triggers_in_category

    dialog = _open(nicegui_client, _picker(nicegui_client, ADX))

    assert _picker_category(dialog) != 'all'
    assert ADX in _entry_keys(dialog)
    assert set(_entry_keys(dialog)) == {
        e.value for e in triggers_in_category(_picker_category(dialog))}


def _picker_category(dialog):
    """The category the modal is showing, read off the chip that is drawn as selected."""
    selected = [c for c in _marked(dialog, TriggerTypePicker.MARKER_CHIP)
                if c._props.get('selected')]
    assert len(selected) == 1, f'{len(selected)} chips selected'
    prefix = f'{TriggerTypePicker.MARKER_CHIP}-'
    categories = [m[len(prefix):] for m in selected[0]._markers if m.startswith(prefix)]
    assert len(categories) == 1, categories
    return categories[0]


def test_a_value_the_catalog_does_not_know_opens_on_all(nicegui_client):
    """No category holds it, and an EMPTY list would read as "this platform has no triggers"."""
    from ba2_common.core.trigger_catalog import trigger_catalog

    dialog = _open(nicegui_client, _picker(nicegui_client, 'a_trigger_from_a_future_release'))

    assert _picker_category(dialog) == 'all'
    assert len(_entry_keys(dialog)) == len(trigger_catalog())


def test_clicking_a_row_sets_the_value_and_closes_the_modal(nicegui_client):
    """Picking IS the action -- there is no Save button to press afterwards, because a
    picker with one is a dialog the operator has to dismiss twice."""
    changed = []
    picker = _picker(nicegui_client, HAS_POSITION, on_change=lambda: changed.append(picker.value))
    dialog = _open(nicegui_client, picker, category='all')

    _click(_entry_card(dialog, CONFIDENCE))

    assert picker.value == CONFIDENCE
    assert _marked_texts(picker.root, TriggerTypePicker.MARKER_VALUE_KEY) == [CONFIDENCE]
    assert changed == [CONFIDENCE], (
        'the row must be told, or the operator/value controls keep the old trigger\'s shape')
    assert dialog.value is False, 'the modal must close on the pick'


def test_re_picking_the_trigger_already_selected_leaves_the_row_alone(nicegui_client):
    """A REGRESSION the old control could not have. ``ui.select`` fires no change event when the
    value does not change; the picker's ``pick`` fired ``on_change`` unconditionally, and
    ``on_change`` rebuilds the operator/value controls from the ORIGINAL trigger_config. So
    opening ``confidence > 80``, typing 85, reopening the picker and clicking ``confidence``
    again -- the row that is already selected, the safest thing in the dialog to click -- threw
    the 85 away and silently restored 80."""
    changed = []
    picker = _picker(nicegui_client, CONFIDENCE, on_change=lambda: changed.append(picker.value))
    dialog = _open(nicegui_client, picker)

    _click(_entry_card(dialog, CONFIDENCE))

    assert picker.value == CONFIDENCE
    assert dialog.value is False, 'it must still CLOSE -- picking is the action either way'
    assert changed == [], 'no rebuild, so whatever the operator typed survives'


def test_escape_and_the_backdrop_cancel_the_pick(nicegui_client):
    """No Cancel button either: the modal must be non-persistent so Quasar's own Escape and
    backdrop handling dismisses it, leaving the trigger untouched."""
    picker = _picker(nicegui_client, HAS_POSITION)
    dialog = _open(nicegui_client, picker)

    assert not dialog._props.get('persistent')
    with nicegui_client:
        dialog.close()
    assert picker.value == HAS_POSITION


def test_reopening_the_modal_forgets_the_last_search(nicegui_client):
    """A stale query would show a short list that looks like a short CATALOG. The category,
    however, is re-derived from the value that was just picked."""
    picker = _picker(nicegui_client, HAS_POSITION)
    dialog = _open(nicegui_client, picker)
    _click(_chip(dialog, 'market'))
    with nicegui_client:
        picker.search.value = 'adx'
        picker.dialog.close()

    dialog = _open(nicegui_client, picker)

    assert not picker.search.value
    assert len(_entry_keys(dialog)) > 1


def test_the_modal_is_built_once_however_often_it_is_opened(nicegui_client):
    """Rebuilding it per click leaks a whole dialog tree into the page on every open --
    the rule dialog is long-lived and a trigger row is clicked repeatedly while authoring."""
    picker = _picker(nicegui_client, HAS_POSITION)
    _open(nicegui_client, picker)
    first = picker.dialog
    with nicegui_client:
        picker.dialog.close()
    _open(nicegui_client, picker)

    assert picker.dialog is first
    assert len(_marked(nicegui_client.layout, TriggerTypePicker.MARKER_DIALOG)) == 1


def test_the_two_helpers_the_picker_replaces_are_gone():
    """``_authorable_trigger_types`` was the market filter, now replaced by the per-entry
    message; ``_trigger_type_options`` existed only to dodge ``ui.select``'s ValueError, and
    a button has no options list to be outside of. Left in place they would read as a second,
    disagreeing answer to "which triggers may be authored"."""
    assert not hasattr(settings_page, '_authorable_trigger_types')
    assert not hasattr(settings_page, '_trigger_type_options')


# =========================================================================================
# The stored shape -- through the real row and the real save
# =========================================================================================

class _RuleEditor:
    """Just enough of the rules editor to build a trigger row and save it."""

    def __init__(self, subtype='enter_market', name='a rule'):
        self.triggers = {}
        self.actions = {}
        self.rule_name_input = SimpleNamespace(value=name)
        self.rule_subtype_select = SimpleNamespace(value=subtype)
        self.continue_processing_checkbox = SimpleNamespace(value=False)
        self.rules_dialog = SimpleNamespace(close=lambda: None)
        self.saved = []
        self._add_trigger_row = TradeSettingsTab._add_trigger_row.__get__(self)
        self._save_rule = TradeSettingsTab._save_rule.__get__(self)
        self._refuse_market_gates_on_exit_rule = (
            TradeSettingsTab._refuse_market_gates_on_exit_rule.__get__(self))

    def _update_rules_table(self):
        pass


@pytest.fixture
def editor(nicegui_client, monkeypatch):
    """A rule editor drawing into a real slot, with the database write intercepted."""
    tab = _RuleEditor()
    monkeypatch.setattr(settings_page, 'add_instance', lambda row: tab.saved.append(row) or 1)
    monkeypatch.setattr(settings_page, 'update_instance', lambda row: tab.saved.append(row))
    notes = []
    monkeypatch.setattr(settings_page.ui, 'notify',
                        lambda msg, **kw: notes.append((str(msg), kw.get('type'))))
    tab.notifications = notes
    with nicegui_client:
        tab.triggers_container = settings_page.ui.column()
    return tab


def _row(editor, client, trigger_config=None):
    with client:
        editor._add_trigger_row(None, trigger_config)
    return editor.triggers[next(reversed(editor.triggers))]['type_picker']


def test_picking_a_flag_trigger_saves_event_type_and_nothing_else(editor, nicegui_client):
    """The stored shape is what the live engine and every exported payload read. A flag
    carries no operator and no value; writing ``operator: '>'`` beside ``has_position``
    would invent a threshold the engine never evaluates."""
    picker = _row(editor, nicegui_client)
    dialog = _open(nicegui_client, picker)
    _click(_entry_card(dialog, HAS_POSITION))

    with nicegui_client:
        editor._save_rule()

    assert len(editor.saved) == 1
    assert list(editor.saved[0].triggers.values()) == [{'event_type': HAS_POSITION}]


def test_picking_a_numeric_trigger_keeps_its_operator_and_value(editor, nicegui_client):
    """The controls beside the trigger are untouched by this change -- and they must be
    REBUILT when the pick changes the trigger's kind, or a flag keeps the previous
    trigger's threshold."""
    picker = _row(editor, nicegui_client, {'event_type': HAS_POSITION})
    dialog = _open(nicegui_client, picker, category='all')
    _click(_entry_card(dialog, CONFIDENCE))

    refs = editor.triggers[next(reversed(editor.triggers))]
    with nicegui_client:
        refs['operator_select']().value = '>='
        refs['value_input']().value = '80'
        editor._save_rule()

    assert list(editor.saved[0].triggers.values()) == [
        {'event_type': CONFIDENCE, 'operator': '>=', 'value': 80.0}]


def test_an_existing_trigger_row_opens_on_its_persisted_value(editor, nicegui_client):
    """The value expression is the ORIGINAL one, character for character.

    Including the legacy ``type`` spelling, which rows written before ``event_type`` still
    carry -- reading those as "new row" would silently relabel them.

    And including the MALFORMED row: ``event_type`` present but null. Quietly turning that null
    into ``has_position`` would relabel a broken rule as a position check, which is a different
    change wearing this one's name -- and the editor is the only door onto fixing such a row, so
    it has to show what is actually stored. (That property lost its test when
    ``_trigger_type_options`` went; it is pinned here now, against the picker.)
    """
    assert _row(editor, nicegui_client, {'event_type': CONFIDENCE}).value == CONFIDENCE
    assert _row(editor, nicegui_client, {'type': 'days_opened'}).value == 'days_opened'
    assert _row(editor, nicegui_client, None).value == HAS_POSITION
    # A null event_type does NOT fall through to the legacy ``type`` and does NOT become the
    # default: the key is present, so null is what the row says.
    assert _row(editor, nicegui_client, {'event_type': None, 'type': 'confidence'}).value is None
    # An empty config is a NEW row, which is what the Add Trigger button hands in.
    assert _row(editor, nicegui_client, {}).value == HAS_POSITION


def test_a_deployed_market_gate_can_still_be_opened_and_saved_unchanged(editor,
                                                                        nicegui_client):
    """A gate that arrived by deploy import must be inspectable, and re-saving the rule
    must not quietly drop or rewrite it."""
    editor.rule_subtype_select.value = 'enter_market'
    picker = _row(editor, nicegui_client, {'event_type': ADX, 'operator': '>', 'value': 25.0})

    assert picker.value == ADX
    with nicegui_client:
        editor._save_rule()
    assert list(editor.saved[0].triggers.values()) == [
        {'event_type': ADX, 'operator': '>', 'value': 25.0}]


# =========================================================================================
# The refusals
# =========================================================================================

#: What a gated open-positions rule may and may not DO (plan 2026-09-24 Task B2).
BUY_ACTIONS = {'action_0': {'action_type': 'buy'}}
CLOSE_ACTIONS = {'action_0': {'action_type': 'close'}}
REFUSED = 'may not use'


def _gate_rule(subtype, event_type=ADX, actions=BUY_ACTIONS):
    tab = _RuleEditor(subtype=subtype, name='exit on adx')
    tab.triggers_data = {'cond_0': {'event_type': event_type, 'operator': '>', 'value': 25.0}}
    tab.actions_data = actions
    return tab


@pytest.fixture
def linked_rulesets(monkeypatch):
    """``rule id -> the rulesets holding it``, as the live link table would answer."""
    links = {}
    monkeypatch.setattr(settings_page, 'rulesets_for_event_action',
                        lambda rule_id: links.get(rule_id, []))
    return links


def _ruleset(name, subtype):
    return SimpleNamespace(id=hash(name) % 1000, name=name,
                           subtype=SimpleNamespace(value=subtype))


def test_a_market_gate_on_an_open_positions_buy_rule_is_refused_at_rule_save():
    """The gap Part 3 closes, narrowed by plan 2026-09-24 Task B2: on the exit pass a market read
    that fails is unknown and the rule does not fire, which is safe for a close/reduce/TP-SL
    adjustment and for nothing else. A gated OPEN on the exit pass is still refused."""
    tab = _gate_rule('open_positions')

    with pytest.raises(ValueError) as excinfo:
        tab._refuse_market_gates_on_exit_rule('open_positions', tab.triggers_data,
                                              tab.actions_data)

    msg = str(excinfo.value)
    assert 'cond_0' in msg and REFUSED in msg and "'buy'" in msg
    assert 'exit on adx' in msg, 'the message must name the rule the operator is looking at'


@pytest.mark.parametrize('action', ['close', 'decrease_instrument_share',
                                    'adjust_stop_loss', 'adjust_take_profit'])
def test_a_market_gate_on_an_open_positions_rule_that_closes_reduces_or_adjusts_saves(action):
    """The B2 contract: a market EXIT is authorable in the editor."""
    tab = _gate_rule('open_positions', actions={'action_0': {'action_type': action}})

    assert tab._refuse_market_gates_on_exit_rule('open_positions', tab.triggers_data,
                                                 tab.actions_data) is None


@pytest.mark.parametrize('action', ['stop_processing', 'roll_pmcc_short'])
def test_a_market_gate_beside_a_stop_or_a_roll_is_refused_at_rule_save(action):
    tab = _gate_rule('open_positions', actions={'action_0': {'action_type': 'close'},
                                                'action_1': {'action_type': action}})

    with pytest.raises(ValueError, match=repr(action)):
        tab._refuse_market_gates_on_exit_rule('open_positions', tab.triggers_data,
                                              tab.actions_data)


def test_the_same_gate_on_an_enter_market_rule_saves():
    """Gates DECIDE ENTRY. On the entry rule they are the feature, not the fault -- whether
    the expert's profile serves them is the expert dialog's question, not this one's."""
    tab = _gate_rule('enter_market')

    for subtype in ('enter_market', None, ''):
        assert tab._refuse_market_gates_on_exit_rule(subtype, tab.triggers_data,
                                                     tab.actions_data) is None


def test_an_ordinary_open_positions_rule_still_saves():
    tab = _gate_rule('open_positions', event_type='profit_loss_percent')

    assert tab._refuse_market_gates_on_exit_rule('open_positions', tab.triggers_data,
                                                 tab.actions_data) is None


def test_the_rule_refusal_speaks_the_ruleset_refusal_s_words():
    """One vocabulary for one failure. Two wordings would read as two different problems
    and send the operator looking for two different fixes."""
    import inspect

    src = inspect.getsource(TradeSettingsTab._refuse_market_gates_on_exit_rule)
    assert 'assert_market_rule_actions_live(' in src, (
        'the message must come from market_condition_rules, not be re-typed here')


def test_the_rule_refusal_runs_before_any_write():
    """A refusal after ``update_instance`` leaves the rule persisted carrying the gate it
    just refused -- and the operator reading the error believes nothing was saved. It runs
    AFTER the actions are collected, because what the rule DOES is half the question."""
    import inspect

    src = inspect.getsource(TradeSettingsTab._save_rule)
    guard = src.index('_refuse_market_gates_on_exit_rule(')
    writes = [src.index(c) for c in ('update_instance(rule)', 'add_instance(new_rule)')]
    assert guard < min(writes)
    assert src.index('actions_data[action_id] = action_config') < guard
    call = src[guard:]
    assert 'actions_data' in call[:call.index(')') + 1]


def test_the_refused_rule_is_not_written_and_the_operator_is_told(editor, nicegui_client):
    """End to end through the real save: the ValueError must reach the notification, not
    the log alone, or the Save button appears to do nothing at all."""
    editor.rule_subtype_select.value = 'open_positions'
    editor.actions['a0'] = {'type_select': SimpleNamespace(value='buy')}
    picker = _row(editor, nicegui_client)
    dialog = _open(nicegui_client, picker, category='market')
    _click(_entry_card(dialog, ADX))

    refs = editor.triggers[next(reversed(editor.triggers))]
    with nicegui_client:
        refs['operator_select']().value = '>'
        refs['value_input']().value = '25'
        editor._save_rule()

    assert editor.saved == []
    assert any(REFUSED in msg and "'buy'" in msg and kind == 'negative'
               for msg, kind in editor.notifications), editor.notifications


def test_a_gated_close_rule_is_written_through_the_real_save(editor, nicegui_client):
    """The other half, end to end: the same gate on an open-positions rule that CLOSES saves,
    carrying its gate and its action exactly as authored."""
    editor.rule_subtype_select.value = 'open_positions'
    editor.actions['a0'] = {'type_select': SimpleNamespace(value='close')}
    picker = _row(editor, nicegui_client)
    dialog = _open(nicegui_client, picker, category='market')
    _click(_entry_card(dialog, ADX))

    refs = editor.triggers[next(reversed(editor.triggers))]
    with nicegui_client:
        refs['operator_select']().value = '>'
        refs['value_input']().value = '25'
        editor._save_rule()

    saved, = editor.saved
    assert list(saved.triggers.values()) == [{'event_type': ADX, 'operator': '>', 'value': 25.0}]
    assert saved.actions == {'a0': {'action_type': 'close'}}


def test_the_ruleset_level_refusal_still_fires(monkeypatch):
    """A regression test, not a duplicate: the menu filter that used to make this refusal
    nearly unreachable is gone, so the door it guards is now the one operators walk through."""
    rule = SimpleNamespace(id=2, name='gated', triggers={'cond_0': {'event_type': ADX}},
                           actions=BUY_ACTIONS)
    monkeypatch.setattr(settings_page, 'get_instance', lambda model, rid: rule)
    tab = object.__new__(TradeSettingsTab)
    tab.ruleset_name_input = SimpleNamespace(value='exit rules')

    with pytest.raises(ValueError, match=REFUSED):
        TradeSettingsTab._refuse_market_gates_on_exit_ruleset(tab, 'open_positions', [2])
    rule.actions = CLOSE_ACTIONS
    assert TradeSettingsTab._refuse_market_gates_on_exit_ruleset(
        tab, 'open_positions', [2]) is None


# =========================================================================================
# The operator and value controls beside the trigger
# =========================================================================================

def _operator(editor):
    return editor.triggers[next(reversed(editor.triggers))]['operator_select']()


def _card(editor):
    return editor.triggers[next(reversed(editor.triggers))]['card']


def test_a_categorical_trigger_offers_only_the_operator_the_engine_accepts(editor,
                                                                          nicegui_client):
    """THE FAILURE THIS PINS. ``is_numeric_event('structure_state')`` is True -- it is an ``N_``
    member because the stored value is a float -- so the row offered all six operators and
    ``structure_state > 1`` was authorable. That expression means "bear only" (bull=1, bear=2),
    the OPPOSITE of what somebody who has just read "1 = bull" is trying to say, and nothing
    refused it. ``MarketConditionCompare`` accepts ``==`` alone for a categorical field, so every
    other operator authored a rule that raises the moment the condition is built."""
    from ba2_common.core.trigger_catalog import operator_options_for

    _row(editor, nicegui_client, {'event_type': STATE, 'operator': '==', 'value': 1.0})

    assert list(_operator(editor).options) == operator_options_for(STATE) == ['==']


def test_a_numeric_market_gate_offers_only_the_two_thresholds_the_engine_accepts(
        editor, nicegui_client):
    """The same table's numeric half: a market-condition gate is a strict threshold, and the
    engine refuses ``>=``, ``<=``, ``==`` and ``!=`` on one."""
    _row(editor, nicegui_client, {'event_type': ADX, 'operator': '>', 'value': 25.0})

    assert list(_operator(editor).options) == ['>', '<']


def test_an_ordinary_numeric_trigger_keeps_all_six_operators(editor, nicegui_client):
    """Only the market fields go through ``MarketConditionCompare``. Narrowing ``confidence``
    would take away ``>=``, which live rules use."""
    from ba2_trade_platform.core.types import get_operator_options

    _row(editor, nicegui_client, {'event_type': CONFIDENCE, 'operator': '>=', 'value': 80.0})

    assert list(_operator(editor).options) == get_operator_options()


def test_a_flag_trigger_has_no_operator_or_value_box(editor, nicegui_client):
    _row(editor, nicegui_client, {'event_type': HAS_POSITION})
    refs = editor.triggers[next(reversed(editor.triggers))]

    assert refs['operator_select']() is None and refs['value_input']() is None


def test_the_code_legend_is_shown_beside_the_value_box_of_a_categorical(editor,
                                                                        nicegui_client):
    """The value box takes a regime CODE. Without the legend the operator is typing a number
    whose meaning is nowhere on screen, and 1 vs 2 is bull vs bear."""
    from ba2_common.core.trigger_catalog import categorical_codes_for

    assert categorical_codes_for(STATE), 'no codes registered would make this test vacuous'
    _row(editor, nicegui_client, {'event_type': STATE, 'operator': '==', 'value': 1.0})
    legend = _marked_texts(_card(editor), settings_page.MARKER_TRIGGER_LEGEND)

    assert len(legend) == 1, legend
    for name, code in categorical_codes_for(STATE):
        assert f'{name}={code}' in legend[0], legend[0]


def test_an_ordinary_trigger_gets_no_legend(editor, nicegui_client):
    """A legend on everything is a legend nobody reads."""
    _row(editor, nicegui_client, {'event_type': CONFIDENCE, 'operator': '>', 'value': 80.0})

    assert not _marked(_card(editor), settings_page.MARKER_TRIGGER_LEGEND)


def test_a_persisted_operator_the_engine_refuses_is_shown_and_flagged_not_hidden(
        editor, nicegui_client):
    """``structure_state > 1`` is exactly what this editor used to let somebody author, so rows
    like it can already exist. Dropping ``>`` from the options without keeping the stored value
    would make the rule impossible to OPEN (NiceGUI raises on a select value outside its
    options), and silently rewriting it to ``==`` would change the strategy behind the
    operator's back. It is offered, and it says the engine refuses it."""
    _row(editor, nicegui_client, {'event_type': STATE, 'operator': '>', 'value': 1.0})

    assert _operator(editor).value == '>'
    assert '>' in list(_operator(editor).options)
    assert _marked(_card(editor), settings_page.MARKER_TRIGGER_OPERATOR_REFUSED), (
        'an operator the engine will refuse must not look like an ordinary choice')


# =========================================================================================
# The refusal keys on what the ENGINE reads, not on the rule's own subtype
# =========================================================================================

def test_a_gate_on_a_rule_linked_into_an_open_positions_ruleset_is_refused(linked_rulesets):
    """THE HOLE THIS CLOSES, and it is the whole failure this feature could have introduced.

    ``db.ruleset_event_actions`` loads a ruleset's rules by the LINK alone -- there is no
    ``EventAction.subtype`` filter anywhere on the live read path -- so the rule's own subtype is
    a proxy for where it runs, not the thing the engine reads. Rule R (open_positions, ungated)
    goes into ruleset S (open_positions), S is assigned to an expert's open-positions slot. The
    operator then edits R, flips its Subtype to Enter Market and adds ``underlying_adx_14 > 25``.
    A refusal that keys on R's own subtype sees enter_market and allows it; no ruleset save and
    no expert save happen, so neither of those doors runs; the (S, R) link is untouched. Live, R
    still evaluates on the open-positions pass, reads ``no_context``, never fires -- and the exit
    or protective-order adjustment silently stops happening."""
    tab = _gate_rule('enter_market')
    linked_rulesets[41] = [_ruleset('exit rules', 'open_positions')]

    with pytest.raises(ValueError) as excinfo:
        tab._refuse_market_gates_on_exit_rule('enter_market', tab.triggers_data,
                                              tab.actions_data, 41)

    msg = str(excinfo.value)
    assert REFUSED in msg
    assert 'exit rules' in msg, 'the message must name the ruleset the operator has to go fix'
    assert 'cond_0' in msg


def test_a_gate_on_a_rule_linked_only_into_entry_rulesets_still_saves(linked_rulesets):
    """The link is what decides, in both directions: a gated ENTRY rule sitting in entry
    rulesets is the feature working."""
    tab = _gate_rule('enter_market')
    linked_rulesets[41] = [_ruleset('entry rules', 'enter_market'),
                           _ruleset('more entries', 'enter_market')]

    assert tab._refuse_market_gates_on_exit_rule('enter_market', tab.triggers_data,
                                                 tab.actions_data, 41) is None


def test_the_rules_own_subtype_still_refuses_a_brand_new_unlinked_rule(linked_rulesets):
    """The own-subtype check is kept as well as the link check, not replaced by it: a rule being
    created has no id and no links yet, and its Subtype is the only statement of where it is
    headed."""
    tab = _gate_rule('open_positions')

    with pytest.raises(ValueError, match=REFUSED):
        tab._refuse_market_gates_on_exit_rule('open_positions', tab.triggers_data,
                                              tab.actions_data, None)


def test_an_ungated_rule_in_an_open_positions_ruleset_is_never_asked_about_its_links(
        monkeypatch):
    """No gate, no question: the link table is not read at all for an ordinary rule, so the
    common save does not pay for a query it cannot learn anything from."""
    asked = []
    monkeypatch.setattr(settings_page, 'rulesets_for_event_action',
                        lambda rule_id: asked.append(rule_id) or [])
    tab = _gate_rule('open_positions', event_type='profit_loss_percent')

    assert tab._refuse_market_gates_on_exit_rule('open_positions', tab.triggers_data,
                                                 tab.actions_data, 41) is None
    assert asked == []


def test_the_link_lookup_is_used_by_the_real_save(editor, nicegui_client, linked_rulesets,
                                                  monkeypatch):
    """End to end through ``_save_rule``: the rule is an ENTER MARKET rule, so only the link can
    refuse it, and the refusal has to reach the notification and stop the write."""
    saved_rule = SimpleNamespace(id=41, name='was an exit rule', triggers={}, actions={},
                                 type=None, subtype=None, continue_processing=False)
    editor.rule_subtype_select.value = 'enter_market'
    editor.actions['a0'] = {'type_select': SimpleNamespace(value='buy')}
    linked_rulesets[41] = [_ruleset('exit rules', 'open_positions')]
    picker = _row(editor, nicegui_client)
    dialog = _open(nicegui_client, picker, category='market')
    _click(_entry_card(dialog, ADX))

    refs = editor.triggers[next(reversed(editor.triggers))]
    with nicegui_client:
        refs['operator_select']().value = '>'
        refs['value_input']().value = '25'
        editor._save_rule(saved_rule)

    assert editor.saved == []
    assert any('exit rules' in msg and kind == 'negative'
               for msg, kind in editor.notifications), editor.notifications


def test_the_rule_save_passes_the_rules_id_to_the_refusal():
    """Read from the source: passing ``None`` for an EDIT would leave the link check unable to
    find anything and the refusal back to keying on a proxy."""
    import inspect

    src = inspect.getsource(TradeSettingsTab._save_rule)
    call = src[src.index('_refuse_market_gates_on_exit_rule('):]
    call = call[:call.index(')') + 1]
    assert 'rule.id' in call, call
