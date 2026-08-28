"""The live rule editor must be able to express every option param the action consumes.

WHY (2026-08-23 structure audit): ``min_volume`` and ``wing_width_pct`` were plumbed the
whole way -- ``rule_builders._OPTION_ACTION_PARAM_KEYS``,
``TradeActionEvaluator._OPTION_ENTRY_PARAM_KEYS``, the ``_OptionEntryAction`` ctor -- and had
NO widget in the rule editor, so no live rule could ever set them. Confirmed read-only
against the production DB: all 14 live option entry actions carry min_open_interest and
max_spread_pct, and NOT ONE carries either of those two. A live iron condor therefore ran on
``OpenIronCondorAction.DEFAULT_WING_PCT`` with nothing on screen saying so.

Layer-by-layer tests could not catch that: every layer was correct in isolation and the
PRODUCER was missing. So this test compares the producer's key set against the consumer's.

IT DOES SO BY RUNNING THE EDITOR, NOT BY READING IT (rewritten 2026-08-23). The first version
regexed ``action_config['...'] =`` out of ``inspect.getsource``, which proved only that the
assignment TEXT existed. Three mutations of ``ui/pages/settings.py`` left it green:

  M-U1  delete the ``wing_width_input = ui.number(...)`` widget outright -- the name stays
        None, ``if wwp:`` is False, nothing is persisted, and the assignment text is intact;
  M-U2  turn ``if uses_wing_width(selected_type):`` into a condition that never holds --
        same outcome, same intact text;
  M-U3  drop ``'wing_width_input': lambda: wing_width_input`` from the refs dict -- the save
        path then raises KeyError into ``_save_rule``'s blanket ``except``, which notifies
        and returns having written nothing. Still green, still intact text.

A guard that cannot fail when the feature is deleted is worse than no guard: it advertises
coverage that does not exist. So the row is now really built (a bare ``nicegui.Client``
supplies the slot stack -- no browser, no event loop), the action type is really switched
through the editor's own change handler, ``_save_rule`` is really called, and the assertions
are made on the dict that reaches ``update_instance``. Every mutation above now fails it.
"""
import inspect
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
from ba2_common.core.types import (
    ExpertActionType,
    get_arc_floor_action_values,
    get_wing_width_action_values,
    uses_arc_floor,
    uses_wing_width,
)

WING_ACTION = ExpertActionType.OPEN_IRON_CONDOR.value
PLAIN_OPTION_ACTION = ExpertActionType.BUY_CALL.value


@pytest.fixture(scope="module")
def settings_module():
    """``ba2_trade_platform.ui.pages`` pulls every page (and the expert/LLM stack) on
    import, so pay for it once per module."""
    from ba2_trade_platform.ui.pages import settings

    return settings


@pytest.fixture
def nicegui_client():
    """A slot stack, so the editor's ``ui.*`` calls have somewhere to draw."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page

    client = Client(nicegui_page('/test-option-param-reachability'), request=None)
    yield client
    client.remove_elements(client.elements.values())


class _Editor:
    """The rule dialog's action rows, driven the way a user drives them.

    ``TradeSettingsTab.__init__`` builds the whole settings page; none of that is needed to
    exercise the action row, so the instance is created unbound and given only the handful of
    attributes ``_add_action_row`` / ``_save_rule`` actually read. Everything else -- widget
    construction, the action-type change handler, the save path's parsing -- is the real code.
    """

    def __init__(self, settings, monkeypatch):
        from nicegui import ui

        self._settings = settings
        self.saved = []
        self.errors = []
        monkeypatch.setattr(settings, 'update_instance',
                            lambda rule: self.saved.append(rule.actions))
        monkeypatch.setattr(settings.logger, 'error',
                            lambda msg, *a, **k: self.errors.append(str(msg)))

        tab = object.__new__(settings.TradeSettingsTab)
        tab.actions = {}
        tab.triggers = {}
        tab.actions_container = ui.column()
        tab.rule_name_input = SimpleNamespace(value='reachability rule')
        tab.rule_subtype_select = SimpleNamespace(value=None)
        tab.continue_processing_checkbox = SimpleNamespace(value=False)
        tab.rules_dialog = SimpleNamespace(close=lambda: None)
        tab._update_rules_table = lambda: None
        self.tab = tab

    def add_row(self, action_type, **config):
        self.tab._add_action_row(action_key='a0',
                                 action_config={'action_type': action_type, **config})
        return self

    def widget(self, ref):
        """The widget the save path will read for ``ref`` -- or None if there is none."""
        return self.tab.actions['a0'][ref]()

    def choose(self, action_type):
        """Pick a different action type, then run the editor's OWN change handler.

        Setting ``.value`` alone only updates the model; the rebuild is wired with
        ``action_select.on('update:model-value', ...)``, which in a browser fires next. The
        listener is located by handler qualname rather than by position so that finding zero
        of them is a loud failure rather than a silently skipped rebuild."""
        select = self.tab.actions['a0']['type_select']
        select.value = action_type
        handlers = [listener.handler
                    for listener in select._event_listeners.values()
                    if listener.type in ('update:modelValue', 'update:model-value')
                    and '_add_action_row' in getattr(listener.handler, '__qualname__', '')]
        assert len(handlers) == 1, (
            f"the action row's own change handler is not on the type select: {handlers}")
        handlers[0]()
        return self

    def save(self):
        """Run ``_save_rule`` and return the persisted action config for the row.

        ``_save_rule`` wraps everything in ``except Exception: notify(); return``, so a
        mutation that makes the save path blow up would otherwise look like a test that
        merely found nothing. Both failure shapes are turned into assertion failures here."""
        rule = SimpleNamespace(id=1, name=None, type=None, subtype=None, triggers=None,
                               actions=None, continue_processing=None)
        self.tab._save_rule(rule)
        assert self.errors == [], f"_save_rule swallowed an exception: {self.errors}"
        assert len(self.saved) == 1, "the rule was never handed to update_instance"
        return self.saved[0]['a0']


@pytest.fixture
def editor(settings_module, nicegui_client, monkeypatch):
    with nicegui_client:
        yield _Editor(settings_module, monkeypatch)


# --------------------------------------------------------------------------- #
# wing width: the param that was plumbed everywhere except onto the screen
# --------------------------------------------------------------------------- #
def test_the_editor_really_persists_wing_width_pct(editor):
    """The four multi-leg structures read it; without a widget they silently used a
    constant. Kills M-U1 / M-U2 / M-U3: each leaves the key out of the saved config (or
    blows the save up), and none of them can be papered over by the source still saying
    ``action_config['wing_width_pct'] = ...`` somewhere."""
    saved = editor.add_row(WING_ACTION).save()
    assert 'wing_width_pct' in saved, (
        f"a live {WING_ACTION} still cannot be given a wing width: {sorted(saved)}")


def test_the_persisted_wing_width_is_the_one_the_user_typed(editor):
    """Not a constant that happens to match the default: move the widget, and the saved
    number has to move with it. This is what makes the previous test about the WIDGET
    rather than about the literal 5.0."""
    editor.add_row(WING_ACTION)
    editor.widget('wing_width_input').value = 7.5
    assert editor.save()['wing_width_pct'] == 7.5


def test_an_existing_rules_wing_width_is_loaded_back_into_the_editor(editor):
    """The EDIT path. A param that saves but does not reload is still unusable: opening the
    rule would silently reset it to the default on the next save."""
    saved = editor.add_row(WING_ACTION, wing_width_pct=12.5).save()
    assert editor.widget('wing_width_input').value == 12.5
    assert saved['wing_width_pct'] == 12.5


def test_wing_width_is_offered_for_exactly_the_actions_that_read_it(settings_module,
                                                                    nicegui_client,
                                                                    monkeypatch):
    """Guard the decoy risk in the other direction, by RUNNING each action type: offering
    the field on a structure with no wings would invite a setting that does nothing."""
    for action_type in get_wing_width_action_values():
        with nicegui_client:
            saved = _Editor(settings_module, monkeypatch).add_row(action_type).save()
        assert 'wing_width_pct' in saved, action_type

    non_wing = [a.value for a in ExpertActionType
                if settings_module.is_option_action(a.value)
                and not uses_wing_width(a.value)
                and a is not ExpertActionType.CLOSE_OPTION]
    assert non_wing, "no non-wing option action to check against"
    for action_type in non_wing:
        with nicegui_client:
            saved = _Editor(settings_module, monkeypatch).add_row(action_type).save()
        assert 'wing_width_pct' not in saved, action_type


def test_the_wing_actions_really_have_a_wing_default_to_override():
    """The set must track the builders, not just agree with itself."""
    from ba2_common.core import TradeActions as TA

    by_value = {
        ExpertActionType.OPEN_IRON_CONDOR.value: TA.OpenIronCondorAction,
        ExpertActionType.OPEN_JADE_LIZARD.value: TA.OpenJadeLizardAction,
        ExpertActionType.OPEN_CALL_BUTTERFLY.value: TA.OpenCallButterflyAction,
        ExpertActionType.OPEN_PUT_RATIO_SPREAD.value: TA.OpenPutRatioSpreadAction,
    }
    assert set(get_wing_width_action_values()) == set(by_value)
    for value, cls in by_value.items():
        assert hasattr(cls, "DEFAULT_WING_PCT")


# --------------------------------------------------------------------------- #
# switching the action type must not leave a widget behind
# --------------------------------------------------------------------------- #
def test_switching_off_a_wing_structure_stops_persisting_a_wing_width(editor):
    """THE STALE CLOSURE. ``wing_width_input`` is the only CONDITIONALLY created widget in
    the row -- every other one is built unconditionally on each rebuild, so every other name
    is refreshed. Pick an iron condor, then change your mind and pick a plain long call: the
    row is redrawn without a wing field, but the closure still points at the DELETED number,
    whose ``.value`` is still 5.0, so the save writes ``wing_width_pct`` onto a structure
    that has no wings. Harmless today only because ``BuyCallAction`` ignores it."""
    editor.add_row(WING_ACTION).choose(PLAIN_OPTION_ACTION)
    assert editor.widget('wing_width_input') is None, (
        "the wing widget outlived the row it belonged to")
    saved = editor.save()
    assert saved['action_type'] == PLAIN_OPTION_ACTION
    assert 'wing_width_pct' not in saved, (
        f"a stray wing width was persisted onto {PLAIN_OPTION_ACTION}: {saved}")


def test_switching_ONTO_a_wing_structure_still_offers_the_field(editor):
    """The other direction, so the fix cannot be "never persist it after a switch"."""
    editor.add_row(PLAIN_OPTION_ACTION).choose(WING_ACTION)
    assert editor.widget('wing_width_input') is not None
    assert 'wing_width_pct' in editor.save()


def test_switching_between_two_wing_structures_keeps_the_field(editor):
    editor.add_row(WING_ACTION).choose(ExpertActionType.OPEN_JADE_LIZARD.value)
    assert 'wing_width_pct' in editor.save()


def test_switching_to_close_option_persists_no_entry_params(editor):
    """CLOSE_OPTION resolves its contract from the held position and takes no parameters;
    every entry widget is stale by then."""
    saved = editor.add_row(WING_ACTION).choose(ExpertActionType.CLOSE_OPTION.value).save()
    assert set(saved) == {'action_type'}, saved


# --------------------------------------------------------------------------- #
# the producer's key set vs the consumer's
# --------------------------------------------------------------------------- #
def test_the_editor_persists_every_param_it_is_expected_to(editor):
    """Compare what the editor can actually produce with what the action really consumes."""
    editor.add_row(WING_ACTION)
    editor.widget('strike_param_input').value = '0.30'   # blank by default, so nothing saves
    editor.widget('min_arc_input').value = 150.0         # ditto -- an absent floor is not 0
    saved = editor.save()

    persisted = set(saved) - {'action_type'}
    consumed = set(_OPTION_ENTRY_PARAM_KEYS)
    # min_volume is the ONE deliberate omission: AlpacaAccount builds its chain from the
    # option SNAPSHOT endpoint, whose payload has no bar, so OptionContract.volume is always
    # None on the live path and the gate could only ever raise. See the note at the save site.
    #
    # strike_method is not omitted, it is PER-ACTION: WING_ACTION (iron condor) hard-codes
    # percent_otm at every selection site, so offering the field there is the OPT-S2 trap.
    # Its reachability on the nine actions that DO read it is asserted immediately below,
    # so this exclusion cannot hide the field disappearing everywhere.
    #
    # entry_cross is the second deliberate omission, for the same KIND of reason as
    # min_volume: it is a fraction of the SIMULATOR's modelled bid-ask spread, and only a
    # simulator has one (``_OptionEntryAction._modelled_half_spreads`` duck-types
    # ``option_modelled_half_spread``, which exists on BacktestAccount and on no live
    # account). A live account has real quotes and its builders already quote at the real
    # touch -- buy@ask / sell@bid IS a full concession -- so a live editor field could only
    # ever be a control that changes nothing. See core.option_entry_quote.
    expected = consumed - {'min_volume', 'strike_method', 'entry_cross'}
    assert expected <= persisted, f"live rules cannot set: {sorted(expected - persisted)}"
    assert 'min_volume' not in persisted
    assert 'strike_method' not in persisted
    assert persisted <= consumed, (
        f"the editor writes keys no option action reads: {sorted(persisted - consumed)}")


def test_the_editor_persists_every_param_for_a_structure_that_reads_the_strike_method(
        editor):
    """The other half: on an action that DOES read strike_method, nothing is missing."""
    from ba2_common.core.types import ExpertActionType as _AT

    editor.add_row(_AT.SELL_CASH_SECURED_PUT.value)
    editor.widget('strike_param_input').value = '0.30'
    editor.widget('min_arc_input').value = 15.0
    saved = editor.save()

    persisted = set(saved) - {'action_type'}
    consumed = set(_OPTION_ENTRY_PARAM_KEYS)
    # wing_width_pct is the per-action omission here, mirroring strike_method above;
    # entry_cross is backtest-only (see the note in the sibling test).
    expected = consumed - {'min_volume', 'wing_width_pct', 'entry_cross'}
    assert expected <= persisted, f"live rules cannot set: {sorted(expected - persisted)}"
    assert 'strike_method' in persisted
    assert persisted <= consumed


def test_every_persisted_param_survives_the_trip_into_the_action(editor):
    """The keys are not just present, they are the ones ``TradeActionEvaluator`` forwards to
    the ctor -- which is the layer that made the original defect invisible."""
    from ba2_common.core.TradeActions import create_action

    editor.add_row(WING_ACTION)
    editor.widget('strike_param_input').value = '0.30'
    editor.widget('min_arc_input').value = 150.0
    saved = editor.save()

    kwargs = {k: v for k, v in saved.items() if k in _OPTION_ENTRY_PARAM_KEYS}
    action = create_action(ExpertActionType(WING_ACTION), 'AAPL', SimpleNamespace(),
                           SimpleNamespace(), None, None, **kwargs)
    for key, value in kwargs.items():
        assert getattr(action, key) == value, key


def test_the_entry_cross_omission_is_justified_by_the_live_accounts_themselves():
    """The executable half of the ``entry_cross`` exclusion above.

    ``_OptionEntryAction`` sizes the concession by duck-typing ``option_modelled_half_spread``
    off the account, so an account without it concedes nothing whatever the rule says. If a
    live broker ever grows that seam, this fails and the editor owes the field a widget --
    which is the only thing that would make an exclusion honest rather than a hole.
    """
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount

    for cls in (AlpacaAccount, IBKRAccount, TastyTradeAccount):
        assert not hasattr(cls, "option_modelled_half_spread"), (
            f"{cls.__name__} now models an option spread, so entry_cross is no longer "
            f"backtest-only and the live rule editor must offer it")


def test_the_deliberate_omission_is_documented_where_someone_would_add_it(settings_module):
    """The one assertion that is legitimately about TEXT: it guards a COMMENT, which has no
    runtime behaviour to observe. Everything else in this file runs the code."""
    src = inspect.getsource(settings_module)
    start = src.index("elif is_option_action(action_type):")
    end = src.index("actions_data[action_id] = action_config", start)
    assert "min_volume" in src[start:end], (
        "the reason min_volume has no widget must live next to the other params, or the "
        "next person will 'fix' the gap and ship a field that can only error")


# --------------------------------------------------------------------------- #
# OPT-S2: strike_method — the editor must not OFFER what the builder ignores
# --------------------------------------------------------------------------- #
# Eight of the seventeen entry builders hard-code ``method="percent_otm"`` at every
# selection site, so ``self.strike_method`` is a dead attribute on them. The editor
# nevertheless rendered the Strike Method select for EVERY non-close option action,
# DEFAULTED it to ``delta``, placeholdered Strike Param as ``0.30``, and persisted the
# choice unconditionally. A user configuring an iron condor saw "delta", typed 0.30
# expecting a 30-delta short, and got a strike 0.30 % out of the money — effectively at
# the money, on the leg carrying the risk.
#
# The decision is REFUSAL AT CONFIG TIME, not a fallback: honouring delta in those eight
# depends on whether delta is computable from the chain, which is separate work. Until
# then the field must not be offered and must not be persisted, and the param must say
# what it really means.
IGNORES_STRIKE_METHOD = ExpertActionType.OPEN_IRON_CONDOR.value
HONOURS_STRIKE_METHOD = ExpertActionType.SELL_CASH_SECURED_PUT.value


def test_a_structure_that_ignores_strike_method_is_not_offered_one(editor):
    editor.add_row(IGNORES_STRIKE_METHOD)
    assert editor.widget('strike_method_select') is None, (
        f"{IGNORES_STRIKE_METHOD} hard-codes percent_otm at every selection site, but the "
        f"editor still offers a Strike Method — and it defaults to 'delta'")


def test_a_structure_that_ignores_strike_method_never_persists_one(editor):
    saved = editor.add_row(IGNORES_STRIKE_METHOD).save()
    assert 'strike_method' not in saved, (
        f"the editor persisted strike_method={saved.get('strike_method')!r} onto "
        f"{IGNORES_STRIKE_METHOD}, which never reads it: {sorted(saved)}")


def test_a_structure_that_honours_strike_method_still_gets_one(editor):
    """Refusal must not become "remove the feature": the nine that read it keep it."""
    editor.add_row(HONOURS_STRIKE_METHOD)
    assert editor.widget('strike_method_select') is not None
    saved = editor.save()
    assert saved['strike_method'] == 'delta'


def test_strike_method_is_offered_for_exactly_the_actions_that_read_it(
        settings_module, nicegui_client, monkeypatch):
    """Both directions, by RUNNING every entry action through the editor."""
    from ba2_common.core.types import (
        get_strike_method_action_values, honours_strike_method,
    )

    entries = [a.value for a in ExpertActionType
               if settings_module.is_option_action(a.value)
               and a is not ExpertActionType.CLOSE_OPTION]
    assert len(entries) == 17, entries

    for action_type in entries:
        with nicegui_client:
            saved = _Editor(settings_module, monkeypatch).add_row(action_type).save()
        if honours_strike_method(action_type):
            assert 'strike_method' in saved, (
                f"{action_type} reads strike_method but no live rule can set it")
        else:
            assert 'strike_method' not in saved, (
                f"{action_type} ignores strike_method, yet the editor persisted "
                f"{saved.get('strike_method')!r}")
    assert set(get_strike_method_action_values()) <= set(entries)


def test_switching_onto_a_structure_that_ignores_it_drops_a_stale_strike_method(editor):
    """The stale-closure shape that already bit ``wing_width_pct``.

    Configure a CSP with a delta, change your mind and pick an iron condor: the row is
    redrawn without the select, and the deleted widget's closure must not write ``delta``
    onto a structure that will silently read it as a percentage.
    """
    editor.add_row(HONOURS_STRIKE_METHOD).choose(IGNORES_STRIKE_METHOD)
    assert editor.widget('strike_method_select') is None
    saved = editor.save()
    assert saved['action_type'] == IGNORES_STRIKE_METHOD
    assert 'strike_method' not in saved, saved


def test_switching_onto_a_structure_that_honours_it_offers_it_again(editor):
    editor.add_row(IGNORES_STRIKE_METHOD).choose(HONOURS_STRIKE_METHOD)
    assert editor.widget('strike_method_select') is not None
    assert 'strike_method' in editor.save()


def test_an_existing_rules_strike_method_is_loaded_back_for_an_action_that_reads_it(editor):
    saved = editor.add_row(HONOURS_STRIKE_METHOD, strike_method='percent_otm').save()
    assert editor.widget('strike_method_select').value == 'percent_otm'
    assert saved['strike_method'] == 'percent_otm'


def test_an_existing_rule_carrying_a_dead_strike_method_is_cleaned_on_save(editor):
    """A rule saved BEFORE this fix carries a strike_method the builder ignores.

    Re-saving it must not carry the key forward — otherwise the trap survives every edit
    of the rule that introduced it.
    """
    saved = editor.add_row(IGNORES_STRIKE_METHOD, strike_method='delta').save()
    assert 'strike_method' not in saved, (
        f"a dead strike_method survived a re-save of {IGNORES_STRIKE_METHOD}: {saved}")


def test_the_save_path_refuses_a_strike_method_widget_that_should_not_exist(editor):
    """The SECOND rail, exercised — otherwise it is untestable and therefore not a rail.

    Not hypothetical: ``wing_width_pct`` is the row's other conditional widget and its
    closure OUTLIVED the row, so a value from a previously-selected action type was
    persisted onto a structure with no such field (see the stale-closure test above).
    ``strike_method`` escapes that today only because the rebuild happens to reset its
    name. Here the widget is forced to exist for a structure that ignores the setting —
    exactly what a future refactor reusing the widget would produce — and the save must
    still refuse, because the guard is keyed on the ACTION and not on the widget.
    """
    editor.add_row(IGNORES_STRIKE_METHOD)
    assert editor.widget('strike_method_select') is None
    editor.tab.actions['a0']['strike_method_select'] = (
        lambda: SimpleNamespace(value='delta'))
    saved = editor.save()
    assert 'strike_method' not in saved, (
        f"a stray Strike Method widget was enough to persist {saved.get('strike_method')!r} "
        f"onto {IGNORES_STRIKE_METHOD}, which reads it as a percentage")


def test_the_strike_param_says_what_it_means_where_the_method_is_fixed(editor):
    """With no method to choose, the param's label and placeholder must not imply delta.

    ``0.30`` under a label that says nothing is exactly how "30-delta" became "0.30 % OTM".
    """
    editor.add_row(IGNORES_STRIKE_METHOD)
    widget = editor.widget('strike_param_input')
    assert widget is not None
    label = (widget.props.get('label') or '')
    placeholder = (widget.props.get('placeholder') or '')
    assert '%' in label or 'OTM' in label.upper(), (
        f"the strike param for a percent-OTM-only structure is labelled {label!r}")
    assert '0.30' not in placeholder, (
        f"the placeholder still suggests a delta on a structure that cannot use one: "
        f"{placeholder!r}")


# --------------------------------------------------------------------------- #
# min_arc: the premium-richness floor (OPT-C1)
# --------------------------------------------------------------------------- #
def test_an_absent_arc_floor_stays_absent(editor):
    """THE DEFAULT MATTERS MORE THAN THE FIELD. An unset floor means "no richness
    requirement", which is what every live rule has today. A pre-filled 0 would NOT be the
    same thing: ``admits_credit_structure`` treats a configured 0.0 as a gate that still
    refuses every UNMEASURABLE ARC, so a blank field that saved as 0 would quietly start
    declining structures nobody asked it to decline."""
    saved = editor.add_row(WING_ACTION).save()
    assert 'min_arc' not in saved, saved


def test_the_persisted_arc_floor_is_the_one_the_user_typed_converted_to_a_fraction(editor):
    """Percent on screen, FRACTION on the wire -- ``option_economics`` and the GA gene both
    work in fractions, and 150 %/yr read as a 150x floor would refuse everything."""
    editor.add_row(WING_ACTION)
    editor.widget('min_arc_input').value = 150.0
    assert editor.save()['min_arc'] == pytest.approx(1.5)


def test_an_existing_rules_arc_floor_is_loaded_back_into_the_editor(editor):
    """The EDIT path, in the same units: a stored 0.25 must show as 25 %/yr and round-trip
    unchanged, or opening a rule and saving it would silently divide the floor by 100."""
    saved = editor.add_row(WING_ACTION, min_arc=0.25).save()
    assert editor.widget('min_arc_input').value == pytest.approx(25.0)
    assert saved['min_arc'] == pytest.approx(0.25)


def test_the_arc_floor_is_offered_for_exactly_the_actions_that_read_it(settings_module,
                                                                       nicegui_client,
                                                                       monkeypatch):
    """Both directions, by RUNNING each action type. Offering it on a DEBIT structure is
    worse than the wing-width decoy: a debit structure posts no collateral, so its ARC is
    None and any floor at all refuses the entry outright."""
    for action_type in get_arc_floor_action_values():
        with nicegui_client:
            ed = _Editor(settings_module, monkeypatch).add_row(action_type)
            assert ed.widget('min_arc_input') is not None, action_type
            ed.widget('min_arc_input').value = 20.0
            assert 'min_arc' in ed.save(), action_type

    non_credit = [a.value for a in ExpertActionType
                  if settings_module.is_option_action(a.value)
                  and not uses_arc_floor(a.value)
                  and a is not ExpertActionType.CLOSE_OPTION]
    assert non_credit, "no non-credit option action to check against"
    for action_type in non_credit:
        with nicegui_client:
            ed = _Editor(settings_module, monkeypatch).add_row(action_type)
            assert ed.widget('min_arc_input') is None, action_type
            assert 'min_arc' not in ed.save(), action_type


def test_switching_off_a_credit_structure_stops_persisting_an_arc_floor(editor):
    """THE STALE CLOSURE again -- ``min_arc_input`` is the row's second conditionally
    created widget, and the first one (wing width) is on record for outliving its row. Pick
    an iron condor, set a floor, change your mind to a long call: the floor must not follow.
    On a debit structure it would not merely be inert, it would be read by nothing at all,
    but the stored rule would claim a criterion it does not have."""
    editor.add_row(WING_ACTION)
    editor.widget('min_arc_input').value = 150.0
    editor.choose(PLAIN_OPTION_ACTION)
    assert editor.widget('min_arc_input') is None
    saved = editor.save()
    assert saved['action_type'] == PLAIN_OPTION_ACTION
    assert 'min_arc' not in saved, saved


def test_the_arc_action_list_matches_the_builders_that_consult_the_gate():
    """The list must track ``TradeActions``, not just agree with itself: a ninth credit
    builder that calls the gate but is missing here would be unreachable from a live rule,
    and a name here whose builder does not call it would be an inert field."""
    import inspect

    from ba2_common.core import TradeActions as TA

    by_value = {
        ExpertActionType.SELL_CASH_SECURED_PUT.value: TA.SellCashSecuredPutAction,
        ExpertActionType.OPEN_BEAR_CALL_SPREAD.value: TA.OpenBearCallSpreadAction,
        ExpertActionType.OPEN_BULL_PUT_SPREAD.value: TA.OpenBullPutSpreadAction,
        ExpertActionType.OPEN_SHORT_STRADDLE.value: TA.OpenShortStraddleAction,
        ExpertActionType.OPEN_SHORT_STRANGLE.value: TA.OpenShortStrangleAction,
        ExpertActionType.OPEN_IRON_CONDOR.value: TA.OpenIronCondorAction,
        ExpertActionType.OPEN_JADE_LIZARD.value: TA.OpenJadeLizardAction,
        ExpertActionType.OPEN_PUT_RATIO_SPREAD.value: TA.OpenPutRatioSpreadAction,
    }
    assert set(get_arc_floor_action_values()) == set(by_value)
    for value, cls in by_value.items():
        assert "_refuse_if_arc_below_floor" in inspect.getsource(cls), value
