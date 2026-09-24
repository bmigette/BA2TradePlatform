"""The Edit/Add Ruleset dialog: the rules it holds, in the order it evaluates them.

WHAT IT REPLACES. The dialog rendered EVERY rule whose subtype matched -- dozens of
them -- each with a checkbox, as one scrolling list. A ruleset of two rules was authored
by hunting two ticks among dozens of irrelevant ones, and nothing on screen said that the
sequence of those ticks decides which rule fires.

THE BUG IT FIXES. ``_save_ruleset`` collected the ticked ids by walking
``self.selected_rules``, a dict built in the order of ``get_all_instances(EventAction)``
-- the GLOBAL rule list, i.e. id order. It then deleted every ``RulesetEventActionLink``
and rewrote them with ``order_index = enumerate(that order)``. ``TradeActionEvaluator``
stops at the FIRST rule whose conditions pass unless that rule is marked
``continue_processing``, so ``order_index`` IS the precedence: opening a ruleset whose
stored order differed from id order and pressing Save silently re-ranked which rule fires.
``test_a_stored_order_that_differs_from_id_order_survives_an_untouched_save`` is that
regression; the rest pin the shape that makes the order visible and editable.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

import ba2_trade_platform.ui.pages.settings as settings_page
from ba2_trade_platform.core.db import (add_instance, get_all_instances, get_db,
                                        get_instance, update_instance)
from ba2_trade_platform.core.models import EventAction, Ruleset, RulesetEventActionLink
from ba2_trade_platform.core.types import AnalysisUseCase, ExpertEventRuleType
from ba2_trade_platform.ui.pages.settings import TradeSettingsTab
from ba2_trade_platform.ui.utils import ruleset_picker

ENTER = AnalysisUseCase.ENTER_MARKET.value
EXIT = AnalysisUseCase.OPEN_POSITIONS.value


# --------------------------------------------------------------------------- scaffolding

@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw. No browser.

    Not decoration: NiceGUI resolves a widget's parent from a per-task slot stack, and
    building one with that stack empty raises "The current slot cannot be determined".
    In the real page the Settings tab supplies the stack.
    """
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-ruleset-rule-picker'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _marked(root, marker):
    return [d for d in root.descendants(include_self=True)
            if marker in (getattr(d, '_markers', None) or ())]


def _marked_texts(root, marker):
    return [d.text for d in _marked(root, marker)]


def _click(element):
    """Fire an element's own click listener, with no browser and no event loop.

    The listener list is snapshotted because the handler REDRAWS the container the
    element sits in -- an arrow click rebuilds the whole card list -- and iterating a
    dict while the click deletes its owner raises instead of clicking.
    """
    fired = 0
    for listener in list(element._event_listeners.values()):
        if listener.type.split('.')[0] == 'click':
            listener.handler(None)
            fired += 1
    assert fired, f'no click handler on {element}'


def _rule(name, subtype=ENTER, triggers=None, actions=None, continues=False):
    """A stored rule. Its id is handed out by the database, so id order is arrival order."""
    rule_id = add_instance(EventAction(
        name=name,
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase(subtype),
        triggers=triggers if triggers is not None else {
            'trigger_0': {'event_type': 'confidence', 'operator': '>', 'value': 70.0}},
        actions=actions if actions is not None else {'action_0': {'action_type': 'buy'}},
        extra_parameters={},
        continue_processing=continues))
    return get_instance(EventAction, rule_id)


def _ruleset(name, rule_ids, subtype=ENTER):
    """A stored ruleset whose links carry ``rule_ids`` as their ``order_index``."""
    ruleset_id = add_instance(Ruleset(name=name, subtype=AnalysisUseCase(subtype)))
    session = get_db()
    for order_index, rule_id in enumerate(rule_ids):
        session.add(RulesetEventActionLink(ruleset_id=ruleset_id, eventaction_id=rule_id,
                                           order_index=order_index))
    session.commit()
    session.close()
    return get_instance(Ruleset, ruleset_id)


def _stored_order(ruleset_id):
    """The rule ids of a ruleset as the EVALUATOR reads them: by ``order_index``."""
    session = get_db()
    links = session.exec(
        select(RulesetEventActionLink)
        .where(RulesetEventActionLink.ruleset_id == ruleset_id)
        .order_by(RulesetEventActionLink.order_index)).all()
    order = [link.eventaction_id for link in links]
    session.close()
    return order


@pytest.fixture
def editor(nicegui_client, monkeypatch):
    """The real tab, without its ``render()``: the dialog under test is all it needs.

    ``__new__`` rather than a stand-in, so every method the dialog calls is the one
    production calls -- a stand-in that forgets to forward a method tests nothing.
    """
    tab = TradeSettingsTab.__new__(TradeSettingsTab)
    with nicegui_client:
        tab.rulesets_dialog = settings_page.ui.dialog()
    tab._update_rulesets_table = lambda: None
    notes = []
    monkeypatch.setattr(settings_page.ui, 'notify',
                        lambda msg, **kw: notes.append((str(msg), kw.get('type'))))
    tab.notifications = notes
    return tab


def _open(editor, nicegui_client, ruleset=None):
    with nicegui_client:
        editor.show_ruleset_dialog(ruleset)
    return editor.rulesets_dialog


def _card_names(dialog):
    return _marked_texts(dialog, TradeSettingsTab.MARKER_RULESET_RULE_NAME)


def _card(dialog, rule_id):
    cards = _marked(dialog, f'{TradeSettingsTab.MARKER_RULESET_RULE}-{rule_id}')
    assert len(cards) == 1, f'rule {rule_id}: {len(cards)} cards'
    return cards[0]


def _button(root, marker):
    from nicegui import ui
    buttons = [e for e in _marked(root, marker) if isinstance(e, ui.button)]
    assert len(buttons) == 1, f'{marker}: {len(buttons)} buttons'
    return buttons[0]


def _open_add_modal(editor, nicegui_client):
    with nicegui_client:
        _click(_button(editor.rulesets_dialog, TradeSettingsTab.MARKER_RULESET_ADD))
    modal = editor.ruleset_add_dialog
    assert TradeSettingsTab.MARKER_RULESET_ADD_DIALOG in modal._markers
    return modal


def _search(modal, query):
    """Type into the modal's search box, found the way a test must find it: by its mark."""
    boxes = _marked(modal, TradeSettingsTab.MARKER_RULESET_ADD_SEARCH)
    assert len(boxes) == 1, f'{len(boxes)} search boxes'
    boxes[0].value = query


def _candidate_names(modal):
    return _marked_texts(modal, TradeSettingsTab.MARKER_RULESET_CANDIDATE_NAME)


def _tick(modal, rule_id):
    ticks = _marked(modal, f'{TradeSettingsTab.MARKER_RULESET_CANDIDATE}-{rule_id}')
    assert len(ticks) == 1, f'rule {rule_id}: {len(ticks)} candidate ticks'
    ticks[0].value = True


# =========================================================================================
# The pure half: which rules are offered, what an arrow does, what a subtype change costs
# =========================================================================================

def test_a_rule_already_in_the_ruleset_is_not_offered_again():
    """THE POINT OF THE MODAL. Re-offering what is already listed is how the old
    all-rules list read: the operator had to remember which ticks were theirs."""
    a, b, c = _rule('a'), _rule('b'), _rule('c')

    offered = ruleset_picker.addable_rules([a, b, c], ENTER, already=[b.id])

    assert [r.name for r in offered] == ['a', 'c']


def test_only_rules_of_the_rulesets_own_subtype_are_offered():
    """A ruleset is evaluated for ONE use case; a rule of another subtype in it is a rule
    that can never fire."""
    enter, exit_rule = _rule('enter-1', ENTER), _rule('exit-1', EXIT)

    assert [r.name for r in ruleset_picker.addable_rules(
        [enter, exit_rule], ENTER, already=[])] == ['enter-1']
    assert [r.name for r in ruleset_picker.addable_rules(
        [enter, exit_rule], EXIT, already=[])] == ['exit-1']


def test_search_matches_the_rule_name_and_the_words_of_its_gates():
    """Rules are named by people and gated by the engine's vocabulary. An operator
    hunting "the one that moves the stop" has only the second to search on."""
    named = _rule('take-profit-ladder')
    gated = _rule('rule-17', actions={'a0': {'action_type': 'adjust_stop_loss',
                                             'reference_value': 'order_open_price',
                                             'value': -8.0}})
    rules = [named, gated]

    assert [r.name for r in ruleset_picker.search_rules(rules, 'ladder')] == ['take-profit-ladder']
    assert [r.name for r in ruleset_picker.search_rules(rules, 'stop loss')] == ['rule-17']
    assert [r.name for r in ruleset_picker.search_rules(rules, '')] == ['take-profit-ladder',
                                                                       'rule-17']


def test_an_arrow_at_the_end_of_the_list_moves_nothing():
    """The first rule has nothing above it. Wrapping around would be a re-rank nobody
    asked for, on the one control whose entire job is to be deliberate."""
    assert ruleset_picker.moved([7, 8, 9], 0, -1) == [7, 8, 9]
    assert ruleset_picker.moved([7, 8, 9], 2, +1) == [7, 8, 9]


def test_an_arrow_swaps_a_rule_with_its_neighbour():
    assert ruleset_picker.moved([7, 8, 9], 1, -1) == [8, 7, 9]
    assert ruleset_picker.moved([7, 8, 9], 1, +1) == [7, 9, 8]


def test_a_subtype_change_separates_the_rules_that_no_longer_match():
    """Kept in order, dropped reported. The caller must be able to NAME what it lost --
    a count alone leaves the operator to guess which two rules went."""
    enter_a, exit_b, enter_c = _rule('a', ENTER), _rule('b', EXIT), _rule('c', ENTER)

    kept, dropped = ruleset_picker.partition_by_subtype([enter_a, exit_b, enter_c], ENTER)

    assert [r.name for r in kept] == ['a', 'c']
    assert [r.name for r in dropped] == ['b']


# =========================================================================================
# The dialog
# =========================================================================================

def test_the_dialog_lists_only_the_rules_this_ruleset_holds(editor, nicegui_client):
    """The whole complaint, in one assertion: opening a ruleset of two shows two rules,
    not every rule in the platform that happens to share its subtype."""
    mine_a, mine_b = _rule('mine-a'), _rule('mine-b')
    for other in range(5):
        _rule(f'someone-elses-{other}')
    ruleset = _ruleset('two-rules', [mine_a.id, mine_b.id])

    dialog = _open(editor, nicegui_client, ruleset)

    assert _card_names(dialog) == ['mine-a', 'mine-b']


def test_every_card_is_open_showing_its_gates_and_its_actions(editor, nicegui_client):
    """No fold toggle: the list is short now, and a rule folded shut is a rule the
    operator has to click to find out whether it is the one they meant."""
    rule = _rule('entry',
                 triggers={'t0': {'event_type': 'confidence', 'operator': '>=', 'value': 80.0}},
                 actions={'a0': {'action_type': 'buy'}})
    ruleset = _ruleset('one-rule', [rule.id])

    dialog = _open(editor, nicegui_client, ruleset)
    texts = [d.text for d in dialog.descendants() if getattr(d, 'text', None)]

    assert 'confidence >= 80' in texts, texts
    assert 'Buy' in texts, texts
    assert any('first match wins' in t for t in texts), (
        'order IS precedence, and a list that does not say so reads as "all of these apply"')


def test_the_add_modal_offers_matching_rules_that_are_not_already_listed(editor,
                                                                        nicegui_client):
    inside = _rule('already-in')
    outside = _rule('not-in-yet')
    wrong_subtype = _rule('an-exit-rule', EXIT)
    ruleset = _ruleset('rs', [inside.id])

    _open(editor, nicegui_client, ruleset)
    modal = _open_add_modal(editor, nicegui_client)

    assert _candidate_names(modal) == ['not-in-yet']
    assert 'already-in' not in _candidate_names(modal)
    assert wrong_subtype.name not in _candidate_names(modal)


def test_the_add_modals_search_box_narrows_the_candidates(editor, nicegui_client):
    _rule('momentum-entry')
    _rule('mean-reversion-entry')
    ruleset = _ruleset('rs', [])

    _open(editor, nicegui_client, ruleset)
    modal = _open_add_modal(editor, nicegui_client)
    with nicegui_client:
        _search(modal, 'momentum')

    assert _candidate_names(modal) == ['momentum-entry']


def test_several_ticked_candidates_are_added_in_one_confirm(editor, nicegui_client):
    """Multi-select is the reason this is a modal and not a menu: a ruleset is authored
    in one pass, not one round trip per rule."""
    first, second, untouched = _rule('first'), _rule('second'), _rule('untouched')
    ruleset = _ruleset('rs', [])

    dialog = _open(editor, nicegui_client, ruleset)
    modal = _open_add_modal(editor, nicegui_client)
    _tick(modal, first.id)
    _tick(modal, second.id)
    with nicegui_client:
        _click(_button(modal, TradeSettingsTab.MARKER_RULESET_ADD_CONFIRM))

    assert _card_names(dialog) == ['first', 'second']
    assert untouched.name not in _card_names(dialog)


def test_a_tick_survives_a_change_of_the_search_box(editor, nicegui_client):
    """Searching REBUILDS the candidate list. A tick that lived only in the checkbox was
    deleted by the next keystroke -- silently, in the one control whose whole purpose is
    to collect several rules before confirming."""
    momentum, reversion = _rule('momentum-entry'), _rule('mean-reversion-entry')
    ruleset = _ruleset('rs', [])

    dialog = _open(editor, nicegui_client, ruleset)
    modal = _open_add_modal(editor, nicegui_client)
    with nicegui_client:
        _search(modal, 'momentum')
        _tick(modal, momentum.id)
        _search(modal, 'reversion')
        _tick(modal, reversion.id)
        _click(_button(modal, TradeSettingsTab.MARKER_RULESET_ADD_CONFIRM))

    assert sorted(_card_names(dialog)) == ['mean-reversion-entry', 'momentum-entry']


def test_the_arrows_change_the_order_and_the_order_is_what_gets_saved(editor,
                                                                     nicegui_client):
    """The arrows are the ONLY thing that re-ranks a ruleset. That is the whole contract:
    precedence changes when someone presses an arrow, and at no other moment."""
    a, b, c = _rule('a'), _rule('b'), _rule('c')
    ruleset = _ruleset('rs', [a.id, b.id, c.id])

    dialog = _open(editor, nicegui_client, ruleset)
    with nicegui_client:
        _click(_button(_card(dialog, c.id), TradeSettingsTab.MARKER_RULESET_UP))
    assert _card_names(editor.rulesets_dialog) == ['a', 'c', 'b']

    with nicegui_client:
        editor._save_ruleset(ruleset)

    assert _stored_order(ruleset.id) == [a.id, c.id, b.id]


def test_the_down_arrow_moves_a_rule_later_in_the_precedence(editor, nicegui_client):
    """The other arrow, clicked through the real button. Its sign lives in the wiring, not
    in ``moved`` -- a ``-1`` typed there passes every test of the pure helper."""
    a, b = _rule('a'), _rule('b')
    ruleset = _ruleset('rs', [a.id, b.id])

    dialog = _open(editor, nicegui_client, ruleset)
    with nicegui_client:
        _click(_button(_card(dialog, a.id), TradeSettingsTab.MARKER_RULESET_DOWN))
        editor._save_ruleset(ruleset)

    assert _stored_order(ruleset.id) == [b.id, a.id]


def test_opening_a_second_ruleset_forgets_the_first_ones_rules(editor, nicegui_client):
    """The dialog is ONE object, reopened. A list carried over from the last ruleset would
    be written onto this one by the next Save."""
    mine, theirs = _rule('mine'), _rule('theirs')
    first, second = _ruleset('first', [mine.id]), _ruleset('second', [theirs.id])

    _open(editor, nicegui_client, first)
    dialog = _open(editor, nicegui_client, second)
    with nicegui_client:
        editor._save_ruleset(second)

    assert _card_names(dialog) == ['theirs']
    assert _stored_order(second.id) == [theirs.id]
    assert _stored_order(first.id) == [mine.id], 'and the first ruleset is untouched'


def test_a_stored_order_that_differs_from_id_order_survives_an_untouched_save(
        editor, nicegui_client):
    """THE REGRESSION. ``_save_ruleset`` rebuilt the links from the GLOBAL rule list's
    order -- id order -- so opening a ruleset ranked [c, a, b] and pressing Save with no
    other edit rewrote it as [a, b, c]. The evaluator is first-match, so that silently
    changed which rule fires. Latent when found (no ruleset in prod or dev diverged), and
    this is the test that keeps it that way."""
    a, b, c = _rule('a'), _rule('b'), _rule('c')
    ruleset = _ruleset('hand-ranked', [c.id, a.id, b.id])
    assert _stored_order(ruleset.id) != sorted(_stored_order(ruleset.id)), (
        'the fixture must diverge from id order or this test proves nothing')

    _open(editor, nicegui_client, ruleset)
    with nicegui_client:
        editor._save_ruleset(ruleset)

    assert _stored_order(ruleset.id) == [c.id, a.id, b.id]


def test_a_brand_new_ruleset_is_written_with_the_order_it_was_built_in(editor,
                                                                      nicegui_client):
    """The other half of the save path. A new ruleset has no links to read back, so its
    order comes entirely from the list -- including an arrow pressed before the first
    Save, which must not be forgotten between building and writing."""
    first, second = _rule('first'), _rule('second')

    dialog = _open(editor, nicegui_client, None)
    modal = _open_add_modal(editor, nicegui_client)
    _tick(modal, first.id)
    _tick(modal, second.id)
    with nicegui_client:
        _click(_button(modal, TradeSettingsTab.MARKER_RULESET_ADD_CONFIRM))
        _click(_button(_card(dialog, second.id), TradeSettingsTab.MARKER_RULESET_UP))
        editor.ruleset_name_input.value = 'built-here'
        editor._save_ruleset(None)

    stored = [r for r in get_all_instances(Ruleset) if r.name == 'built-here']
    assert len(stored) == 1, 'the new ruleset was not written'
    assert _stored_order(stored[0].id) == [second.id, first.id]


def test_removing_a_rule_unlinks_it_and_leaves_the_rule_itself_alone(editor,
                                                                    nicegui_client):
    """A rule is SHARED. Deleting the ``EventAction`` from here would gut every other
    ruleset holding it, so the × unlinks and nothing else."""
    kept, removed = _rule('kept'), _rule('removed')
    ruleset = _ruleset('rs', [kept.id, removed.id])
    other_ruleset = _ruleset('another-ruleset-that-shares-it', [removed.id])

    dialog = _open(editor, nicegui_client, ruleset)
    with nicegui_client:
        _click(_button(_card(dialog, removed.id), TradeSettingsTab.MARKER_RULESET_REMOVE))
        editor._save_ruleset(ruleset)

    assert _stored_order(ruleset.id) == [kept.id]
    assert get_instance(EventAction, removed.id) is not None, 'the rule row must survive'
    assert _stored_order(other_ruleset.id) == [removed.id], 'and so must the other ruleset'


def test_changing_the_subtype_drops_the_rules_that_no_longer_match_and_says_so(
        editor, nicegui_client):
    """Never silently, never kept. A rule of another use case cannot fire in this
    ruleset, so keeping it would be a rule that looks live and is not -- and dropping it
    without a word is a ruleset that quietly lost a rule between opening and saving."""
    entry_rule, shared_name = _rule('entry-only'), _rule('exit-too', EXIT).name
    ruleset = _ruleset('rs', [entry_rule.id])

    _open(editor, nicegui_client, ruleset)
    editor.ruleset_subtype_select.value = EXIT
    with nicegui_client:
        editor._on_ruleset_subtype_change()

    assert _card_names(editor.rulesets_dialog) == []
    message, level = editor.notifications[-1]
    assert 'entry-only' in message, message
    # The subtype is named the way the SELECT beside it names it. The raw value is the
    # engine's spelling, and a toast that says ``open_positions`` next to a box that says
    # "Open Positions" reads as a different setting than the one that was just changed.
    assert 'Open Positions' in message, message
    assert EXIT not in message, f'the machine value leaked into the toast: {message}'
    assert level in ('warning', 'negative'), f'a dropped rule is not good news: {level}'
    assert shared_name, 'a rule of the new subtype exists but is not auto-added'


def test_a_ruleset_with_no_subtype_keeps_the_rules_it_holds(editor, nicegui_client):
    """``Ruleset.subtype`` is nullable -- the table prints "Not set" and the rules importer
    writes whatever the payload carries. Read as None the rule filter matches NOTHING, so
    such a ruleset would open empty and a Save would delete every link, while the Subtype
    select beside it cheerfully showed Enter Market. One reading of the missing value."""
    rule = _rule('an-entry-rule', ENTER)
    ruleset_id = add_instance(Ruleset(name='no-subtype', subtype=None))
    session = get_db()
    session.add(RulesetEventActionLink(ruleset_id=ruleset_id, eventaction_id=rule.id,
                                       order_index=0))
    session.commit()
    session.close()
    ruleset = get_instance(Ruleset, ruleset_id)

    dialog = _open(editor, nicegui_client, ruleset)

    assert _card_names(dialog) == ['an-entry-rule']
    assert editor.ruleset_subtype_select.value == ENTER, (
        'the filter and the select must read the missing subtype the same way')
    with nicegui_client:
        editor._save_ruleset(ruleset)
    assert _stored_order(ruleset_id) == [rule.id]


def test_a_rule_deleted_while_the_dialog_sat_open_is_dropped_and_reported(editor,
                                                                         nicegui_client):
    """The save writes links from the list, and the list is ids. A dead id would be a link
    to a row that is not there -- and a ruleset quietly one rule shorter."""
    from ba2_trade_platform.core.db import delete_instance

    kept, doomed = _rule('kept'), _rule('deleted-elsewhere')
    ruleset = _ruleset('rs', [kept.id, doomed.id])

    _open(editor, nicegui_client, ruleset)
    delete_instance(doomed)
    with nicegui_client:
        editor._save_ruleset(ruleset)

    assert _stored_order(ruleset.id) == [kept.id]
    assert any('no longer exist' in message for message, _ in editor.notifications), \
        editor.notifications


def test_an_empty_ruleset_says_so_instead_of_showing_a_blank_panel(editor, nicegui_client):
    """A blank area reads as a screen that failed to load. It also has to name the way
    out, since "Add rules" is now the only way a rule gets in."""
    _rule('available-but-not-added')
    ruleset = _ruleset('empty', [])

    dialog = _open(editor, nicegui_client, ruleset)
    empty = _marked_texts(dialog, TradeSettingsTab.MARKER_RULESET_EMPTY)

    assert len(empty) == 1 and 'Add rules' in empty[0], empty


def test_the_modal_tells_nothing_left_to_add_apart_from_nothing_matching_the_search(
        editor, nicegui_client):
    """Two different facts. Telling an operator "no rule matches" when they have simply
    added them all sends them hunting for a rule already on the other side of the screen."""
    rule = _rule('the-only-entry-rule')
    ruleset = _ruleset('rs', [rule.id])

    _open(editor, nicegui_client, ruleset)
    modal = _open_add_modal(editor, nicegui_client)
    exhausted = _marked_texts(modal, TradeSettingsTab.MARKER_RULESET_ADD_EMPTY)

    assert len(exhausted) == 1 and 'already in this ruleset' in exhausted[0], exhausted

    _rule('another-entry-rule')
    _open(editor, nicegui_client, ruleset)
    modal = _open_add_modal(editor, nicegui_client)
    with nicegui_client:
        _search(modal, 'nothing-is-called-this')
    no_match = _marked_texts(modal, TradeSettingsTab.MARKER_RULESET_ADD_EMPTY)

    assert len(no_match) == 1 and 'matches that search' in no_match[0], no_match


def test_a_tick_the_modal_can_no_longer_honour_is_reported_not_swallowed(editor,
                                                                        nicegui_client):
    """A confirm that quietly adds fewer rules than were ticked is a ruleset that goes
    live missing a rule nobody knows is missing."""
    gone = _rule('an-exit-rule', EXIT)
    ruleset = _ruleset('rs', [])

    dialog = _open(editor, nicegui_client, ruleset)
    modal = _open_add_modal(editor, nicegui_client)
    editor.ruleset_candidate_checked = {gone.id}
    with nicegui_client:
        _click(_button(modal, TradeSettingsTab.MARKER_RULESET_ADD_CONFIRM))

    assert _card_names(dialog) == []
    message, level = editor.notifications[-1]
    assert 'were not added' in message and level == 'warning', message


def test_a_linked_rule_whose_subtype_no_longer_matches_is_dropped_on_open_and_reported(
        editor, nicegui_client):
    """A link the dialog cannot honour. The Rules tab lets a rule's subtype be edited
    after it was linked, and an import can write any link at all -- and such a rule can
    never fire here. The old dialog dropped it too, by never drawing it and rewriting the
    links without it; the only thing new is that the operator is told."""
    good, stale = _rule('still-entry', ENTER), _rule('now-an-exit-rule', EXIT)
    ruleset = _ruleset('rs', [good.id, stale.id], subtype=ENTER)

    dialog = _open(editor, nicegui_client, ruleset)

    assert _card_names(dialog) == ['still-entry']
    message, level = editor.notifications[-1]
    assert 'now-an-exit-rule' in message and level == 'warning', message


def test_a_market_gate_on_an_open_positions_ruleset_is_still_refused(editor,
                                                                    nicegui_client):
    """UNCHANGED BY THIS REWRITE, and checked against the rules the save actually links.
    The gated rule here OPENS (the factory's default ``buy``): since plan 2026-09-24 Task B2 a
    market gate on an exit rule may only close, reduce or adjust TP/SL, because a failed read
    on the exit pass is unknown and the rule does not fire."""
    from ba2_common.core.market_condition_rules import market_condition_fields

    field = sorted(market_condition_fields())[0]
    gate = _rule('regime-gate', EXIT,
                 triggers={'t0': {'event_type': field, 'operator': '>', 'value': 20.0}})
    ruleset = _ruleset('exit-rs', [gate.id], subtype=EXIT)

    _open(editor, nicegui_client, ruleset)
    with pytest.raises(ValueError):
        editor._refuse_market_gates_on_exit_ruleset(EXIT, editor.ruleset_rule_ids)

    # ...and the save reports it rather than writing a ruleset whose exit cannot fire.
    editor.ruleset_name_input.value = 'renamed-by-a-refused-save'
    with nicegui_client:
        editor._save_ruleset(ruleset)
    assert editor.notifications[-1][1] == 'negative', editor.notifications
    # REFUSED BEFORE THE WRITE, which is the whole point: a negative toast over a ruleset
    # that was written anyway would be a refusal in name only. Nothing about the ruleset
    # may have moved -- not its links, not its name.
    assert _stored_order(ruleset.id) == [gate.id], 'the links were rewritten by a refusal'
    assert get_instance(Ruleset, ruleset.id).name == 'exit-rs', (
        'the ruleset row was written by a refusal')


def test_a_market_gate_on_an_open_positions_close_rule_is_accepted(editor, nicegui_client):
    """Plan 2026-09-24 Task B2, against stored rules: the same gate on an exit rule that only
    CLOSES is a market exit, and the ruleset door lets it through."""
    from ba2_common.core.market_condition_rules import market_condition_fields

    field = sorted(market_condition_fields())[0]
    gate = _rule('market-close', EXIT,
                 triggers={'t0': {'event_type': field, 'operator': '>', 'value': 20.0}},
                 actions={'action_0': {'action_type': 'close'}})
    ruleset = _ruleset('market-exit-rs', [gate.id], subtype=EXIT)

    _open(editor, nicegui_client, ruleset)
    assert editor._refuse_market_gates_on_exit_ruleset(EXIT, editor.ruleset_rule_ids) is None
    # ...and, with its id, still accepted: the ruleset is linked to no expert.
    assert editor._refuse_market_gates_on_exit_ruleset(EXIT, editor.ruleset_rule_ids,
                                                       ruleset.id) is None


# -------------------------------------------- the experts ALREADY using the ruleset being saved

@pytest.fixture
def expert_profiles(monkeypatch):
    """``instance id -> market_condition_profile``, served through the instance-resolver seam
    the check reads (the live host backs it with ``get_expert_instance_from_id``)."""
    from types import SimpleNamespace

    from ba2_common.core import instance_resolver

    table: dict = {}
    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(SimpleNamespace(
        get_expert_instance=lambda iid: SimpleNamespace(
            settings={'market_condition_profile': table[iid]})))
    yield table
    instance_resolver.set_instance_resolver(previous)


def _market_close_exit_ruleset(name):
    from ba2_common.core.market_condition_rules import market_condition_fields

    field = 'underlying_adx_14'
    assert field in market_condition_fields()
    gate = _rule(f'{name}-close', EXIT,
                 triggers={'t0': {'event_type': field, 'operator': '>', 'value': 20.0}},
                 actions={'action_0': {'action_type': 'close'}})
    return gate, _ruleset(name, [gate.id], subtype=EXIT)


def _link_expert(ruleset, profiles, profile, *, enabled=True):
    from ba2_trade_platform.core.models import ExpertInstance

    instance_id = add_instance(ExpertInstance(account_id=1, expert='MockExpert', enabled=enabled,
                                              open_positions_ruleset_id=ruleset.id))
    profiles[instance_id] = profile
    return instance_id


@pytest.mark.parametrize('enabled', [True, False])
def test_a_market_exit_saved_into_a_ruleset_an_unprofiled_expert_uses_is_refused(
        editor, nicegui_client, expert_profiles, enabled):
    """The expert dialog checks the profile when a ruleset is ATTACHED; editing a ruleset an
    expert already runs passed through no such check, and the new exit read no_context for
    ever. Disabled experts count: enabling one passes through no ruleset door."""
    _gate, ruleset = _market_close_exit_ruleset(f'linked-unserved-{enabled}')
    instance_id = _link_expert(ruleset, expert_profiles, '', enabled=enabled)

    _open(editor, nicegui_client, ruleset)
    with pytest.raises(ValueError) as e:
        editor._refuse_market_gates_on_exit_ruleset(EXIT, editor.ruleset_rule_ids, ruleset.id)
    msg = str(e.value)
    assert f'expert instance {instance_id}' in msg and 'underlying_adx_14' in msg

    # ...and the real save refuses it before writing anything.
    editor.ruleset_name_input.value = 'renamed-by-a-refused-save'
    with nicegui_client:
        editor._save_ruleset(ruleset)
    assert editor.notifications[-1][1] == 'negative', editor.notifications
    assert get_instance(Ruleset, ruleset.id).name == f'linked-unserved-{enabled}'


def test_a_market_exit_saved_into_a_ruleset_its_expert_serves_is_accepted(
        editor, nicegui_client, expert_profiles):
    _gate, ruleset = _market_close_exit_ruleset('linked-served')
    _link_expert(ruleset, expert_profiles, 'ohlcv-v1')

    _open(editor, nicegui_client, ruleset)
    assert editor._refuse_market_gates_on_exit_ruleset(EXIT, editor.ruleset_rule_ids,
                                                       ruleset.id) is None


def test_an_ordinary_exit_ruleset_save_asks_about_no_expert(editor, nicegui_client,
                                                           expert_profiles, monkeypatch):
    import ba2_common.core.market_condition_live as live

    looked_up = []
    monkeypatch.setattr(live, 'experts_linked_to_rulesets',
                        lambda *a, **k: looked_up.append(a) or ())
    rule = _rule('plain-stop', EXIT,
                 triggers={'t0': {'event_type': 'profit_loss_percent', 'operator': '<',
                                  'value': -5.0}},
                 actions={'action_0': {'action_type': 'close'}})
    ruleset = _ruleset('plain-exit-rs', [rule.id], subtype=EXIT)
    _link_expert(ruleset, expert_profiles, '')

    _open(editor, nicegui_client, ruleset)
    assert editor._refuse_market_gates_on_exit_ruleset(EXIT, editor.ruleset_rule_ids,
                                                       ruleset.id) is None
    assert looked_up == []


# =========================================================================================
# What the save path actually does, and what it says while doing it
# =========================================================================================

def _resubtype(rule, subtype):
    """Change a rule's subtype behind the open dialog, the way the Rules tab would."""
    rule.subtype = AnalysisUseCase(subtype)
    update_instance(rule)
    return rule


def test_the_drop_notice_promises_nothing_is_written_only_where_that_is_true(
        editor, nicegui_client):
    """THE SAME DROP, TWO DIFFERENT TRUTHS. On open and on a subtype change the links are
    still in the database and Cancel puts everything back -- so "Nothing is written until
    you Save" is a fact. On the SAVE path the drop is the last thing that happens before
    the links are rewritten, and the operator read that same reassurance one line above
    "Ruleset saved successfully!" while the link had just been deleted for good."""
    entry_rule = _rule('entry-only')
    _rule('an-exit-rule', EXIT)
    ruleset = _ruleset('rs', [entry_rule.id])

    _open(editor, nicegui_client, ruleset)
    editor.ruleset_subtype_select.value = EXIT
    with nicegui_client:
        editor._on_ruleset_subtype_change()

    pending, _ = editor.notifications[-1]
    assert 'entry-only' in pending, pending
    assert ruleset_picker.NOTHING_WRITTEN_YET in pending, pending

    # ...and now the same drop, on the save, where it IS written.
    stale = _rule('re-subtyped-elsewhere', ENTER)
    saved_ruleset = _ruleset('rs2', [stale.id])
    _open(editor, nicegui_client, saved_ruleset)
    _resubtype(stale, EXIT)
    with nicegui_client:
        editor._save_ruleset(saved_ruleset)

    applied = [m for m, _ in editor.notifications if 're-subtyped-elsewhere' in m][-1]
    assert ruleset_picker.NOTHING_WRITTEN_YET not in applied, (
        f'the save path reassured the operator about a link it was deleting: {applied}')
    assert _stored_order(saved_ruleset.id) == [], 'the drop was in fact written'


def test_a_rule_the_save_drops_is_unlinked_and_not_deleted(editor, nicegui_client):
    """A rule is SHARED. The save-path drop is the one drop that writes, so it is the one
    that could take the ``EventAction`` row with it and gut every other ruleset holding
    it. It unlinks, exactly as the remove button does."""
    stale = _rule('re-subtyped-elsewhere', ENTER)
    ruleset = _ruleset('rs', [stale.id])
    elsewhere = _ruleset('another-ruleset-that-shares-it', [stale.id], subtype=ENTER)

    _open(editor, nicegui_client, ruleset)
    _resubtype(stale, EXIT)
    with nicegui_client:
        editor._save_ruleset(ruleset)

    assert _stored_order(ruleset.id) == []
    assert get_instance(EventAction, stale.id) is not None, 'the rule row must survive'
    assert _stored_order(elsewhere.id) == [stale.id], 'and so must the other ruleset'


def test_a_refused_save_redraws_the_list_it_just_shortened(editor, nicegui_client):
    """THE ARROWS ACT ON A POSITION CAPTURED AT RENDER TIME. The save shortens
    ``ruleset_rule_ids`` (a rule deleted or re-subtyped under the dialog), and a refusal
    leaves the dialog open -- so without a redraw the cards on screen are the pre-drop
    ones while the list behind them is shorter, and the up arrow on the card showing one
    rule moves a different one."""
    from ba2_common.core.market_condition_rules import market_condition_fields
    from ba2_trade_platform.core.db import delete_instance

    field = sorted(market_condition_fields())[0]
    gate = _rule('regime-gate', EXIT,
                 triggers={'t0': {'event_type': field, 'operator': '>', 'value': 20.0}})
    doomed = _rule('deleted-elsewhere', EXIT)
    ruleset = _ruleset('exit-rs', [gate.id, doomed.id], subtype=EXIT)

    dialog = _open(editor, nicegui_client, ruleset)
    assert _card_names(dialog) == ['regime-gate', 'deleted-elsewhere']
    delete_instance(doomed)
    with nicegui_client:
        editor._save_ruleset(ruleset)

    assert editor.notifications[-1][1] == 'negative', 'the save must have been refused'
    assert editor.ruleset_rule_ids == [gate.id], 'the list behind the cards shortened'
    assert _card_names(dialog) == ['regime-gate'], (
        'the cards still show a rule the list no longer holds, so every arrow below it '
        'now moves the wrong rule')


def test_a_failed_link_write_leaves_the_ruleset_row_exactly_as_it_was(
        editor, nicegui_client, monkeypatch):
    """SUBTYPE AND LINKS AGREE, OR THE SAVE DID NOT HAPPEN. Written in two transactions,
    a ruleset whose subtype commits and whose link rewrite then fails (SQLite "database is
    locked" from JobManager is the realistic one) is left labelled one use case and linked
    to rules of another -- a live exit ruleset that can never fire. One session, one
    commit, so a failure below leaves the row untouched."""
    from ba2_trade_platform.core import models as core_models

    kept, removed = _rule('kept'), _rule('removed')
    ruleset = _ruleset('original-name', [kept.id, removed.id])

    dialog = _open(editor, nicegui_client, ruleset)
    with nicegui_client:
        _click(_button(_card(dialog, removed.id), TradeSettingsTab.MARKER_RULESET_REMOVE))
    editor.ruleset_name_input.value = 'renamed'

    def _locked(**_kwargs):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(core_models, 'RulesetEventActionLink', _locked)
    with nicegui_client:
        editor._save_ruleset(ruleset)

    assert editor.notifications[-1][1] == 'negative', editor.notifications
    assert get_instance(Ruleset, ruleset.id).name == 'original-name', (
        'the ruleset row was committed although its links never were')
    assert _stored_order(ruleset.id) == [kept.id, removed.id], (
        'the old links must still be there -- the delete was part of the same transaction')


def test_a_new_ruleset_is_not_written_at_all_when_its_links_cannot_be(
        editor, nicegui_client, monkeypatch):
    """The create path has the same split and the same fix: a Ruleset row committed
    without the links it was built from is an empty ruleset nobody asked for, sitting in
    the table under a name the operator thinks they saved rules under."""
    from ba2_trade_platform.core import models as core_models

    rule = _rule('first')

    _open(editor, nicegui_client, None)
    modal = _open_add_modal(editor, nicegui_client)
    _tick(modal, rule.id)
    with nicegui_client:
        _click(_button(modal, TradeSettingsTab.MARKER_RULESET_ADD_CONFIRM))
    editor.ruleset_name_input.value = 'never-written'

    def _locked(**_kwargs):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(core_models, 'RulesetEventActionLink', _locked)
    with nicegui_client:
        editor._save_ruleset(None)

    assert editor.notifications[-1][1] == 'negative', editor.notifications
    assert [r for r in get_all_instances(Ruleset) if r.name == 'never-written'] == [], (
        'a ruleset row survived a save whose links were never written')


def test_a_tick_the_modal_can_no_longer_honour_is_reported_by_name(editor, nicegui_client):
    """The rows are still there, so the names are recoverable -- and a raw id tells an
    operator nothing about which of the rules they ticked did not make it in."""
    gone = _rule('an-exit-rule', EXIT)
    ruleset = _ruleset('rs', [])

    _open(editor, nicegui_client, ruleset)
    modal = _open_add_modal(editor, nicegui_client)
    editor.ruleset_candidate_checked = {gone.id}
    with nicegui_client:
        _click(_button(modal, TradeSettingsTab.MARKER_RULESET_ADD_CONFIRM))

    message, level = editor.notifications[-1]
    assert 'an-exit-rule' in message and level == 'warning', message


def test_opening_a_ruleset_with_no_stored_subtype_says_it_is_being_read_as_enter_market(
        editor, nicegui_client):
    """``Ruleset.subtype`` is nullable, and the dialog reads a missing one as Enter
    Market. Said out loud, once: such a ruleset can be wired into an expert's
    open-positions slot, where that default makes the drop take EVERY rule it holds and a
    Save write an empty Enter Market ruleset over a live exit."""
    rule = _rule('an-entry-rule', ENTER)
    ruleset_id = add_instance(Ruleset(name='no-subtype', subtype=None))
    session = get_db()
    session.add(RulesetEventActionLink(ruleset_id=ruleset_id, eventaction_id=rule.id,
                                       order_index=0))
    session.commit()
    session.close()

    _open(editor, nicegui_client, get_instance(Ruleset, ruleset_id))

    notices = [m for m, _ in editor.notifications if ruleset_picker.SUBTYPE_MISSING in m]
    assert len(notices) == 1, editor.notifications

    # ...and a ruleset that HAS a subtype is not nagged about one.
    editor.notifications.clear()
    _open(editor, nicegui_client, _ruleset('has-one', [rule.id]))
    assert [m for m, _ in editor.notifications
            if ruleset_picker.SUBTYPE_MISSING in m] == [], editor.notifications
