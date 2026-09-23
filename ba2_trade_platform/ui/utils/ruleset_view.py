"""Rendering a ruleset: what each rule fires ON, and what it DOES.

Everything above ``render_clause`` is pure except ``ruleset_rule_views``, which loads a
stored ruleset's rules from the database, and that is where every "is this a flag or a
threshold", "can this be printed at all" decision is made -- once, where it can be tested
without a browser. ``render_clause`` at the foot is the single piece of NiceGUI markup,
shared because TWO screens draw these views: the read-only transaction-details dialog
(``ui/pages/live_trades.py``) and the ruleset editor (``ui/pages/settings.py``).

**The sentences are the test platform's.** ``testplatform/frontend/src/pages/
Backtesting.tsx`` prints a rule as WHEN <gate> THEN <action>, and an operator reading a
live position is almost always comparing it against the backtest it was deployed from.
Two vocabularies for one rule engine is a translation step in the reader's head, and
the place that translation goes wrong is exactly the place it matters -- a stop
referenced to the entry versus one referenced to the expert's target price.

What this file deliberately does NOT do is prettify the field names. ``days_opened``
and ``profit_loss_percent`` are the engine's own words: they are what the rule editor
shows, what the backtest shows, and what a GA gene is named after. A friendly-name
table here would be a fourth vocabulary and the first one to go stale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: A rule with no triggers is one the evaluator takes as met -- ``_evaluate_conditions``
#: returns True for an empty set. Printed, because a blank WHEN reads as "never fires".
ALWAYS = 'always'

#: Legacy word-form comparisons -> the engine's symbol. Rows written by older builders
#: carry ``gte``; the current vocabulary is ``>=``. One symbol set on screen, or two
#: rules that differ only by the year they were saved in look like different rules.
COMPARISON_SYMBOLS = {
    'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<=',
    'eq': '==', 'neq': '!=', 'ne': '!=',
}

#: Operators that are not comparisons at all: the leaf is a flag (``has_position``,
#: ``bullish``), and any ``value`` stored beside it is a leftover the engine ignores.
_FLAG_OPERATORS = (None, '', 'is_true', 'is_false')

_ACTION_PHRASES = {
    'buy': 'Buy',
    'sell': 'Sell',
    'close': 'Close position',
}

_BRACKET_PHRASES = {
    'adjust_stop_loss': 'Move stop loss',
    'adjust_take_profit': 'Move take profit',
}

#: The screener settings, in the order the Settings dialog edits them -- so the
#: read-only view and the editable one describe a filter in the same sequence.
SCREENER_KEYS: Tuple[str, ...] = (
    'screener_provider',
    'screener_market_cap_min', 'screener_market_cap_max',
    'screener_volume_min', 'screener_volume_max',
    'screener_float_min', 'screener_float_max',
    'screener_price_min', 'screener_price_max',
    'screener_relative_volume_min',
    'screener_price_drop_pct', 'screener_price_drop_days',
    'screener_max_stocks', 'screener_sort_metric',
    'screener_weinstein_stage2_only',
)

#: Thresholds that are read as an ORDER OF MAGNITUDE rather than as a figure. A market
#: cap of 2000000000 makes the reader count zeroes to learn what "2B" says at a glance.
_MAGNITUDE_KEYS = ('market_cap', 'volume', 'float')


def _number(value: Any) -> str:
    """``30.0`` -> ``30``, ``45.5`` -> ``45.5``, anything else -> ``str``.

    Every stored threshold is a float, and a decimal tail on a figure that counts days
    is noise. A real fraction keeps its digits -- the rounding here is presentational
    only and must never drop information the number actually carries.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return str(int(value)) if float(value).is_integer() else str(value)


def _magnitude(value: Any) -> str:
    """``2000000000`` -> ``2B``. Only above 1,000, so a ratio like 1.3 is untouched."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    for cut, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if abs(float(value)) >= cut:
            return f'{_number(float(value) / cut)}{suffix}'
    return _number(value)


def format_condition(condition: Optional[Dict[str, Any]]) -> str:
    """One trigger leaf as a line. ``''`` when there is nothing to say.

    Tolerant of both key spellings the platform has stored: ``event_type``/``field``
    and ``operator``/``comparison``. A leaf with no field names nothing and returns
    empty so the caller can drop the line -- printing "None" or a bare dash there
    would look like a gate the reader cannot see the terms of.
    """
    if not isinstance(condition, dict):
        return ''
    field = condition.get('event_type') or condition.get('field') or ''
    if not field:
        return ''
    raw_op = condition.get('operator', condition.get('comparison'))
    if raw_op in _FLAG_OPERATORS:
        # A FLAG, and any stored ``value`` is ignored exactly as the engine ignores
        # it. Older rows leaked a 0 onto these; printing it would invent a threshold.
        return str(field)
    operator = COMPARISON_SYMBOLS.get(str(raw_op).lower(), str(raw_op))
    value = condition.get('value', condition.get('threshold'))
    return ' '.join(part for part in (str(field), operator, _number(value))
                    if part not in ('', 'None'))


def format_action(action: Optional[Dict[str, Any]]) -> str:
    """One action as a line: what the rule DOES when its gates pass.

    The bracket actions name their REFERENCE as well as their offset, because that is
    half the meaning -- ``-14%`` of the entry price and ``-14%`` of the expert's target
    price are different prices, and the pair is what the GA tuned.
    """
    if not isinstance(action, dict):
        return 'Exit'
    action_type = action.get('action_type') or action.get('action') or ''
    if action_type in _ACTION_PHRASES:
        return _ACTION_PHRASES[action_type]
    if action_type in _BRACKET_PHRASES:
        value = action.get('value')
        offset = ('' if value is None
                  else f'{"+" if isinstance(value, (int, float)) and value >= 0 else ""}'
                       f'{_number(value)}%')
        target = ' '.join(part for part in (action.get('reference_value'), offset) if part)
        return f'{_BRACKET_PHRASES[action_type]} → {target or "reference"}'
    # An action type this view has never heard of. The raw name, de-underscored, says
    # more than a blank cell and cannot claim anything false about what will happen.
    return str(action_type).replace('_', ' ') if action_type else 'Exit'


@dataclass(frozen=True)
class RuleView:
    """One rule, ready to draw: its name, its gates, its actions.

    ``when`` and ``then`` are tuples of finished lines. ``continues`` is the rule's
    ``continue_processing``: evaluation STOPS at the first matching rule without it, so
    it is the difference between "one of these fires" and "these both fire" and it has
    to be on screen.
    """
    name: str
    when: Tuple[str, ...]
    then: Tuple[str, ...]
    continues: bool = False


def build_rule_views(event_actions: Iterable[Any]) -> List[RuleView]:
    """``EventAction`` rows -> drawable rules, IN THE ORDER GIVEN.

    The order is the precedence: ``ruleset_event_actions`` returns rows by
    ``RulesetEventActionLink.order_index`` and the evaluator is first-match. Re-sorting
    here would silently re-rank the rules the reader is being shown.
    """
    views: List[RuleView] = []
    for position, rule in enumerate(event_actions or [], start=1):
        conditions = (getattr(rule, 'triggers', None) or {}).values()
        actions = (getattr(rule, 'actions', None) or {}).values()
        when = tuple(line for line in (format_condition(c) for c in conditions) if line)
        then = tuple(line for line in (format_action(a) for a in actions) if line)
        views.append(RuleView(
            name=(getattr(rule, 'name', None) or f'Rule {position}'),
            when=when or (ALWAYS,),
            then=then,
            continues=bool(getattr(rule, 'continue_processing', False))))
    return views


@dataclass(frozen=True)
class ScreenerCriterion:
    """One line of "which instruments this expert is even allowed to look at".

    ``key`` travels with ``label`` deliberately. A label alone has cost this platform
    once already: ``screener_market_cap_max`` at 0 means NO CEILING, and reading
    "Max market cap: 0" without the key beside it looks like a filter that admits
    nothing. ``is_default`` separates a value the user chose from one they inherited.
    """
    key: str
    label: str
    value: str
    is_default: bool


def screener_criteria(settings: Optional[Dict[str, Any]],
                      definitions: Optional[Dict[str, Any]]) -> List[ScreenerCriterion]:
    """The screener filter this expert selects its universe with. Pure.

    ``definitions`` is ``MarketExpertInterface._builtin_settings`` -- the source of
    truth for what a screener setting IS and what it falls back to. A stored key with
    no definition is skipped rather than guessed at: there is no label for it and no
    default to compare against, so anything shown would be invented.
    """
    stored = settings or {}
    defined = definitions or {}
    criteria: List[ScreenerCriterion] = []
    for key in SCREENER_KEYS:
        meta = defined.get(key)
        if not meta:
            continue
        is_default = key not in stored or stored.get(key) in (None, '')
        value = meta.get('default') if is_default else stored.get(key)
        if value is None or value == '':
            # Neither set nor defaulted to anything: there is no filter to report.
            continue
        render = _magnitude if any(part in key for part in _MAGNITUDE_KEYS) else _number
        criteria.append(ScreenerCriterion(
            key=key, label=str(meta.get('description', key)),
            value=render(value), is_default=is_default))
    return criteria


def ruleset_rule_views(ruleset_id: Optional[int]) -> Sequence[RuleView]:
    """``build_rule_views`` over a stored ruleset. ``()`` when there is no ruleset.

    The load goes through ``core.db.ruleset_event_actions``, which is the ONE loader
    for this join -- ``Ruleset.event_actions`` is lazy and every accessor hands back a
    detached row, so reading the relationship here would raise instead of returning
    the rules.
    """
    if not ruleset_id:
        return ()
    from ...core.db import ruleset_event_actions

    return tuple(build_rule_views(ruleset_event_actions(ruleset_id)))


# ---------------------------------------------------------------------------
# The one drawing helper. Everything above is pure; this is here because TWO screens
# draw a RuleView -- the transaction-details dialog (``ui/pages/live_trades.py``) and the
# ruleset editor (``ui/pages/settings.py``) -- and a second copy of the markup is how the
# two screens start disagreeing about what a rule says.
# ---------------------------------------------------------------------------

def render_clause(label: str, joiner: str, lines: Optional[Iterable[str]]) -> None:
    """One clause of a rule sentence, one line per item, the joining word in the gutter.

    The gutter is a fixed width so WHEN/THEN and the ANDs beneath them share a right edge
    and every clause body starts at the same x -- which is what lets the eye read DOWN a
    column of rules instead of across each one. In the ruleset editor that column is the
    precedence order, so the alignment is doing real work there.
    """
    from nicegui import ui

    for index, line in enumerate(lines or ()):
        with ui.row().classes('items-baseline gap-2 no-wrap'):
            ui.label(label if index == 0 else joiner) \
                .classes('text-caption text-grey-7 w-12 text-right')
            ui.label(line).classes('text-body2 font-mono')
