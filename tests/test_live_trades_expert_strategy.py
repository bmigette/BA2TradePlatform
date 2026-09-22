"""The Expert Strategy section of the transaction-details dialog.

What it pins: a live trade can be read against the rules that will CLOSE it, in the
same sentences the test platform's Strategy tab prints -- WHEN <gate> THEN <action> --
so an operator comparing a live expert to the backtest it was deployed from is not
translating between two vocabularies in their head.

The formatting is pure and lives in ``ui.utils.ruleset_view``; the dialog only walks
the views it returns. These tests are on the pure half, which is where every "is this
a flag or a threshold", "is this measurable at all" decision actually gets made.
"""
from __future__ import annotations

import pytest

from ba2_trade_platform.ui.utils.ruleset_view import (
    ALWAYS,
    RuleView,
    SCREENER_KEYS,
    build_rule_views,
    format_action,
    format_condition,
    screener_criteria,
)


class _Rule:
    """The two fields ``build_rule_views`` reads off an EventAction row."""

    def __init__(self, name, triggers=None, actions=None, continue_processing=False):
        self.name = name
        self.triggers = triggers or {}
        self.actions = actions or {}
        self.continue_processing = continue_processing


# --- conditions ------------------------------------------------------------

def test_a_flag_condition_renders_as_just_its_name():
    """``bullish`` / ``has_no_position`` carry no operator and no value. Printing a
    stray ``0`` beside one would invent a threshold the engine never evaluates."""
    assert format_condition({'event_type': 'bullish'}) == 'bullish'
    assert format_condition({'event_type': 'has_no_position', 'value': 0}) == 'has_no_position'


def test_a_threshold_condition_renders_field_operator_value():
    assert format_condition(
        {'event_type': 'days_opened', 'operator': '>', 'value': 30.0}) == 'days_opened > 30'
    assert format_condition(
        {'event_type': 'confidence', 'operator': '>=', 'value': 80.0}) == 'confidence >= 80'


def test_a_whole_number_threshold_drops_its_decimal_tail():
    """Every stored threshold is a float; ``days_opened > 30.0`` is noise for a
    figure that counts days. A real fraction keeps its digits."""
    assert format_condition(
        {'event_type': 'profit_loss_percent', 'operator': '>', 'value': 52.0}
    ) == 'profit_loss_percent > 52'
    assert format_condition(
        {'event_type': 'confidence', 'operator': '>', 'value': 45.5}
    ) == 'confidence > 45.5'


def test_word_form_operators_are_spoken_as_symbols():
    """Older rows store ``gte``; the engine's own vocabulary is ``>=``. One symbol
    set in the UI, or two rules that differ only by vintage look different."""
    assert format_condition(
        {'event_type': 'confidence', 'operator': 'gte', 'value': 45.0}) == 'confidence >= 45'
    assert format_condition(
        {'field': 'confidence', 'comparison': 'lt', 'value': 45.0}) == 'confidence < 45'


def test_a_condition_with_no_field_renders_as_nothing():
    """Not "None", not "—": a leaf with no field names nothing, and the caller drops
    the line rather than printing a gap in a list of gates."""
    assert format_condition({}) == ''
    assert format_condition({'operator': '>', 'value': 3}) == ''
    assert format_condition(None) == ''


# --- actions ---------------------------------------------------------------

def test_the_plain_actions_read_as_english():
    assert format_action({'action_type': 'buy'}) == 'Buy'
    assert format_action({'action_type': 'sell'}) == 'Sell'
    assert format_action({'action_type': 'close'}) == 'Close position'


def test_a_bracket_action_names_its_reference_and_its_offset():
    """The reference is half the meaning: -14% of the ENTRY and -14% of the expert's
    TARGET are different prices, and the pair is what the GA actually tuned."""
    assert format_action({'action_type': 'adjust_stop_loss',
                          'reference_value': 'order_open_price',
                          'value': -8.0}) == 'Move stop loss → order_open_price -8%'
    assert format_action({'action_type': 'adjust_take_profit',
                          'reference_value': 'expert_target_price',
                          'value': 0.0}) == 'Move take profit → expert_target_price +0%'


def test_an_unknown_action_is_still_readable():
    """A new action type must not render blank. The raw name, de-underscored, says
    more than nothing and cannot claim something false."""
    assert format_action({'action_type': 'roll_short_leg'}) == 'roll short leg'
    assert format_action({}) == 'Exit'


# --- rules -----------------------------------------------------------------

def test_a_rule_becomes_its_gates_and_its_actions():
    view, = build_rule_views([_Rule(
        'enter-buy-1',
        triggers={'cond_0': {'event_type': 'bullish'},
                  'cond_1': {'event_type': 'has_no_position'}},
        actions={'a0': {'action_type': 'buy'},
                 'a1': {'action_type': 'adjust_take_profit',
                        'reference_value': 'order_open_price', 'value': 12.0}})])

    assert view == RuleView(
        name='enter-buy-1',
        when=('bullish', 'has_no_position'),
        then=('Buy', 'Move take profit → order_open_price +12%'),
        continues=False)


def test_the_gates_of_one_rule_are_ANDed():
    """``_evaluate_conditions`` returns True only when ALL triggers pass, so the
    joiner between them is never a choice the reader has to make."""
    view, = build_rule_views([_Rule('r', triggers={
        'cond_0': {'event_type': 'bullish'},
        'cond_1': {'event_type': 'confidence', 'operator': '>', 'value': 45.0}})])

    assert view.when == ('bullish', 'confidence > 45')


def test_a_rule_with_no_gates_fires_always():
    """An empty ``triggers`` dict is "all zero of my conditions passed" -- the
    evaluator takes it as met. A blank WHEN would read as a rule that never fires."""
    view, = build_rule_views([_Rule('r', actions={'a0': {'action_type': 'close'}})])

    assert view.when == (ALWAYS,)


def test_a_continuing_rule_is_marked_as_one():
    """Evaluation STOPS at the first matching rule unless this is set, so it is the
    difference between "one of these fires" and "these both fire"."""
    views = build_rule_views([_Rule('a', continue_processing=True), _Rule('b')])

    assert [v.continues for v in views] == [True, False]


def test_an_unnamed_rule_falls_back_to_its_position():
    views = build_rule_views([_Rule(''), _Rule(None)])

    assert [v.name for v in views] == ['Rule 1', 'Rule 2']


def test_unrenderable_gates_are_dropped_not_printed_empty():
    view, = build_rule_views([_Rule('r', triggers={
        'cond_0': {'event_type': 'bullish'}, 'cond_1': {'nonsense': True}})])

    assert view.when == ('bullish',)


# --- screener --------------------------------------------------------------

def _definitions():
    return {
        'screener_market_cap_min': {'description': 'Min market cap', 'default': 0.0},
        'screener_market_cap_max': {'description': 'Max market cap', 'default': 0.0},
        'screener_max_stocks': {'description': 'Max stocks', 'default': 20},
        'screener_relative_volume_min': {'description': 'Min relative volume', 'default': 1.0},
    }


def test_the_screener_criteria_come_back_in_display_order():
    criteria = screener_criteria(
        {'screener_max_stocks': 50, 'screener_market_cap_min': 2_000_000_000.0},
        _definitions())
    keys = [c.key for c in criteria]

    assert keys == sorted(keys, key=SCREENER_KEYS.index)


def test_a_criterion_carries_its_key_as_well_as_its_label():
    """The label alone has burned this platform once: ``market_cap_max`` reading
    "no ceiling" at 0 is invisible unless the key it came from is on screen."""
    criterion, = screener_criteria({'screener_max_stocks': 50},
                                   {'screener_max_stocks': {'description': 'Max stocks',
                                                            'default': 20}})

    assert (criterion.key, criterion.label) == ('screener_max_stocks', 'Max stocks')
    assert criterion.value == '50'


def test_a_setting_the_instance_never_set_reports_the_default_AS_a_default():
    """It still applies, so hiding it would understate the filter -- but a value the
    user chose and a value they inherited are different facts."""
    by_key = {c.key: c for c in screener_criteria({'screener_max_stocks': 50},
                                                  _definitions())}

    assert by_key['screener_max_stocks'].is_default is False
    assert by_key['screener_relative_volume_min'].is_default is True
    assert by_key['screener_relative_volume_min'].value == '1'


def test_big_thresholds_are_rendered_compactly():
    """A market cap is read as an ORDER OF MAGNITUDE. 2000000000 makes the reader
    count zeroes to learn something the shape of the number already knows."""
    by_key = {c.key: c for c in screener_criteria(
        {'screener_market_cap_min': 2_000_000_000.0,
         'screener_market_cap_max': 10_000_000_000.0}, _definitions())}

    assert by_key['screener_market_cap_min'].value == '2B'
    assert by_key['screener_market_cap_max'].value == '10B'


def test_a_small_ratio_keeps_its_decimals():
    by_key = {c.key: c for c in screener_criteria(
        {'screener_relative_volume_min': 1.3}, _definitions())}

    assert by_key['screener_relative_volume_min'].value == '1.3'


def test_a_key_the_platform_does_not_define_is_not_invented():
    """The definitions are the source of truth for what a screener setting IS. A
    stored key with no definition has no label and no default to report."""
    assert screener_criteria({'screener_made_up': 3}, _definitions()) == [
        c for c in screener_criteria({'screener_made_up': 3}, _definitions())
        if c.key != 'screener_made_up']


@pytest.mark.parametrize('key', SCREENER_KEYS)
def test_every_display_key_is_a_screener_key(key):
    assert key.startswith('screener_')


# ---------------------------------------------------------------------------
# The section itself, against real stored rows.
#
# The pure half above cannot catch a renderer that formats every rule correctly
# and then draws the exit rules under the entry heading.
# ---------------------------------------------------------------------------

@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw. No browser."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-live-trades-expert-strategy'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _stored_expert(entry_rules=(), exit_rules=()):
    """An ExpertInstance pointing at real enter/open rulesets. Returns the instance."""
    from ba2_common.core.db import get_db
    from ba2_common.core.models import (
        AccountDefinition, EventAction, ExpertInstance, Ruleset, RulesetEventActionLink)
    from ba2_trade_platform.core.types import AnalysisUseCase, ExpertEventRuleType

    with get_db() as session:
        account = AccountDefinition(name='strategy-section-test', provider='StubProvider')
        session.add(account)
        session.commit()
        session.refresh(account)

        ruleset_ids = {}
        for slot, rules, use_case in (
                ('enter', entry_rules, AnalysisUseCase.ENTER_MARKET),
                ('exit', exit_rules, AnalysisUseCase.OPEN_POSITIONS)):
            if not rules:
                ruleset_ids[slot] = None
                continue
            ruleset = Ruleset(name=f'{slot}-ruleset',
                              type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                              subtype=use_case)
            session.add(ruleset)
            session.commit()
            session.refresh(ruleset)
            for index, (name, triggers, actions) in enumerate(rules):
                action = EventAction(
                    type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE, subtype=use_case,
                    name=name, triggers=triggers, actions=actions)
                session.add(action)
                session.commit()
                session.refresh(action)
                session.add(RulesetEventActionLink(ruleset_id=ruleset.id,
                                                   eventaction_id=action.id,
                                                   order_index=index))
            session.commit()
            ruleset_ids[slot] = ruleset.id

        expert = ExpertInstance(account_id=account.id, expert='StubExpert',
                                enter_market_ruleset_id=ruleset_ids['enter'],
                                open_positions_ruleset_id=ruleset_ids['exit'])
        session.add(expert)
        session.commit()
        session.refresh(expert)
        session.expunge(expert)
        return expert


def _draw_section(client, expert, monkeypatch, settings=None):
    """Render the section on a bare tab object and hand back the drawn label texts."""
    from nicegui import ui
    from ba2_trade_platform.ui.pages.live_trades import LiveTradesTab

    class _Interface:
        def __init__(self, values):
            self.settings = values or {}

    monkeypatch.setattr('ba2_trade_platform.ui.pages.live_trades.get_expert_instance_from_id',
                        lambda _id: _Interface(settings))
    tab = object.__new__(LiveTradesTab)      # the section touches no tab state
    with client:
        tab._render_expert_strategy_section(expert)
    return [el.text for el in client.layout.descendants() if isinstance(el, ui.label)]


_BULLISH_BUY = ('enter-buy-1',
                {'cond_0': {'event_type': 'bullish'},
                 'cond_1': {'event_type': 'has_no_position'}},
                {'a0': {'action_type': 'buy'},
                 'a1': {'action_type': 'adjust_stop_loss',
                        'reference_value': 'order_open_price', 'value': -8.0}})
_TIME_EXIT = ('exit-0',
              {'cond_0': {'event_type': 'days_opened', 'operator': '>', 'value': 30.0}},
              {'a0': {'action_type': 'close'}})
_RATING_EXIT = ('exit-1', {'cond_0': {'event_type': 'current_rating_negative'}},
                {'a0': {'action_type': 'close'}})


def test_the_section_draws_both_sides_with_their_counts(nicegui_client, monkeypatch):
    expert = _stored_expert(entry_rules=[_BULLISH_BUY],
                            exit_rules=[_TIME_EXIT, _RATING_EXIT])

    texts = _draw_section(nicegui_client, expert, monkeypatch)

    assert 'Entry Rules (1)' in texts
    assert 'Exit Conditions (2)' in texts
    assert 'days_opened > 30' in texts
    assert 'Move stop loss → order_open_price -8%' in texts


def test_the_rule_sentence_reads_when_then(nicegui_client, monkeypatch):
    expert = _stored_expert(entry_rules=[_BULLISH_BUY])

    texts = _draw_section(nicegui_client, expert, monkeypatch)
    gutter = [t for t in texts if t in ('WHEN', 'AND', 'THEN')]

    # WHEN bullish AND has_no_position THEN Buy AND <the stop>
    assert gutter == ['WHEN', 'AND', 'THEN', 'AND']


def test_the_precedence_is_stated_when_it_can_bite(nicegui_client, monkeypatch):
    """One rule cannot be shadowed by another, so the note would be noise. Two can."""
    one = _draw_section(nicegui_client, _stored_expert(exit_rules=[_TIME_EXIT]), monkeypatch)
    assert not [t for t in one if 'first match' in (t or '')]

    two = _draw_section(nicegui_client,
                        _stored_expert(exit_rules=[_TIME_EXIT, _RATING_EXIT]), monkeypatch)
    assert [t for t in two if 'first match' in (t or '')]


def test_a_transaction_with_no_expert_draws_nothing(nicegui_client):
    from nicegui import ui
    from ba2_trade_platform.ui.pages.live_trades import LiveTradesTab

    tab = object.__new__(LiveTradesTab)
    with nicegui_client:
        tab._render_expert_strategy_section(None)

    assert not [el for el in nicegui_client.layout.descendants() if isinstance(el, ui.label)]


def test_an_expert_with_no_rules_and_no_screener_draws_nothing(nicegui_client, monkeypatch):
    """An empty card promising rules that do not exist is worse than no card."""
    texts = _draw_section(nicegui_client, _stored_expert(), monkeypatch)

    assert texts == []


def test_the_screener_block_appears_only_in_screener_mode(nicegui_client, monkeypatch):
    expert = _stored_expert(entry_rules=[_BULLISH_BUY])

    off = _draw_section(nicegui_client, expert, monkeypatch,
                        settings={'instrument_selection_method': 'expert',
                                  'screener_max_stocks': 50})
    assert not [t for t in off if (t or '').startswith('Screener (')]

    on = _draw_section(nicegui_client, expert, monkeypatch,
                       settings={'instrument_selection_method': 'screener',
                                 'screener_max_stocks': 50})
    assert [t for t in on if (t or '').startswith('Screener (')]
    assert 'screener_max_stocks' in on        # the raw key, beside its description


def test_smart_mode_says_the_rules_are_not_what_closes_the_position(nicegui_client,
                                                                    monkeypatch):
    """In Smart mode the SmartRiskManager runs instead of the TradeManager, and it is
    the TradeManager that evaluates these rules."""
    from nicegui import ui
    from ba2_trade_platform.ui.pages.live_trades import LiveTradesTab

    expert = _stored_expert(exit_rules=[_TIME_EXIT])
    monkeypatch.setattr(
        'ba2_trade_platform.ui.pages.live_trades.get_expert_instance_from_id',
        lambda _id: type('I', (), {'settings': {'risk_manager_mode': 'smart'}})())
    tab = object.__new__(LiveTradesTab)
    with nicegui_client:
        tab._render_expert_strategy_section(expert)

    badges = [el for el in nicegui_client.layout.descendants() if isinstance(el, ui.badge)]
    assert any('Smart' in (badge.text or '') for badge in badges)
