"""Which rules a ruleset holds, and in what order -- the decisions behind the picker.

Pure. ``ui/pages/settings.py`` draws the Edit/Add Ruleset dialog by walking what these
return; every "may this rule be offered", "what does this arrow do", "what does a subtype
change cost" answer is made here, once, where it can be tested without a browser.

THE ORDER IS THE PRECEDENCE. ``TradeActionEvaluator`` stops at the first rule whose
conditions pass unless that rule carries ``continue_processing``, and the sequence it
walks is ``RulesetEventActionLink.order_index``. So the list these functions hand back is
not a display convenience: it is what fires. :func:`moved` returns a NEW list rather than
sorting in place for that reason -- the caller's list must only ever change where the
operator pressed an arrow, and the dialog that owned this before rebuilt the order from
the GLOBAL rule list on every save, silently re-ranking any ruleset whose stored order
differed from id order.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple

from .ruleset_view import build_rule_views

#: Said when the list holds rules that cannot fire under the ruleset's subtype. It NAMES
#: them, because a count alone leaves the operator guessing which rules they just lost and
#: those rules were put there deliberately.
_DROPPED_FOR_SUBTYPE = ('Dropped {count} rule(s) from this list -- they do not match the '
                        '"{subtype}" subtype: {names}. A ruleset only evaluates rules of '
                        'its own use case, so a rule of another subtype in it could never '
                        'fire.')

#: The tail that makes the drop a proposal rather than a deletion. Kept as its own name so
#: a test can assert it is ABSENT from the save-path wording: the sentence is the whole
#: difference between the two messages below, and the bug it guards against is a wording
#: that drifted onto a call site where it was not true.
NOTHING_WRITTEN_YET = 'Nothing is written until you Save.'

#: THE DROP HAS NOT HAPPENED YET -- on open, and when the subtype is changed under the
#: list. The links are still in the database and Cancel leaves the ruleset exactly as it
#: was, so "Removed" would be a false alarm about data that is still there.
DROPPED_FOR_SUBTYPE_PENDING = f'{_DROPPED_FOR_SUBTYPE} {NOTHING_WRITTEN_YET}'

#: THE DROP IS THE WRITE -- the save re-checks the list against the database immediately
#: before rewriting the links, so a rule dropped there is a link being deleted now. This
#: message exists because the pending wording was used here too: the operator read
#: "Nothing is written until you Save." one line above "Ruleset saved successfully!" while
#: that link had in fact just been deleted for good. It says what survives instead -- the
#: EventAction row is shared and is only ever UNLINKED here.
DROPPED_FOR_SUBTYPE_APPLIED = (f'{_DROPPED_FOR_SUBTYPE} This Save removes them from the '
                               'ruleset; the rules themselves are not deleted and stay '
                               'in the Rules tab.')

#: Said once, on opening a ruleset whose ``subtype`` column is NULL (nullable, and the
#: rules importer writes whatever the payload carries). The dialog reads a missing subtype
#: as Enter Market so the list and the Subtype select agree -- but such a ruleset may be
#: wired into an expert's open-positions slot, where that default makes the drop take
#: EVERY rule it holds and a Save write an empty Enter Market ruleset over a live exit.
SUBTYPE_MISSING = ('This ruleset has no subtype stored, so it is being edited as "Enter '
                   'Market" and Save will store that. If it is used as an expert\'s Open '
                   'Positions ruleset, set the Subtype first -- saving it as Enter Market '
                   'would drop every rule it holds.')


def subtype_label(subtype: Optional[str]) -> str:
    """A subtype as the dialog SPELLS it: ``open_positions`` -> ``Open Positions``.

    One spelling for the Subtype select's options and for every message that names a
    subtype. Formatted raw, a toast says ``open_positions`` while the box beside it says
    "Open Positions", and the two read as different settings.
    """
    return str(subtype or '').replace('_', ' ').title()


def subtype_of(rule: Any) -> Optional[str]:
    """A rule's subtype as the string the dialog compares against.

    Rows loaded from the database carry an ``AnalysisUseCase``; rows built in a test or
    handed over from an import carry the bare value. Both are the same subtype, and a
    comparison that only understands one of them silently offers nothing.
    """
    raw = getattr(rule, 'subtype', None)
    return getattr(raw, 'value', raw)


def matches_subtype(rule: Any, subtype: Optional[str]) -> bool:
    """Whether ``rule`` belongs to ``subtype``. A rule with no subtype belongs to none."""
    value = subtype_of(rule)
    return bool(value) and value == subtype


def addable_rules(rules: Iterable[Any], subtype: Optional[str],
                  already: Sequence[int]) -> List[Any]:
    """The candidates the Add-rules modal may offer, in the order given.

    Two filters, and neither is cosmetic. A rule of another subtype cannot fire in this
    ruleset at all. A rule ALREADY in it is the complaint this whole dialog exists to fix
    -- re-offering what the list beside it already shows is how the old all-rules screen
    read, where the operator had to remember which of the dozens of ticks were theirs.
    """
    held = set(already or ())
    # ``rule.id``, not ``getattr(rule, 'id', None)``: a stored EventAction always has one,
    # and a rule with no id would sail through the "not already in it" test and be written
    # as ``eventaction_id=None``. A missing id here is a bug, not a case to absorb.
    return [rule for rule in (rules or ())
            if matches_subtype(rule, subtype) and rule.id not in held]


def _count(n: int, noun: str) -> str:
    return f'{n} {noun}' if n == 1 else f'{n} {noun}s'


def nothing_to_add_message(rules: Iterable[Any], subtype: Optional[str],
                           already: Sequence[int], query: Optional[str]) -> str:
    """What the Add-rules modal says when it has no candidate to show.

    "Every rule with this subtype is already in this ruleset" was TRUE on an instance
    holding one entry rule and four exit rules -- and read as a broken modal, because it
    gave no count and never mentioned the four rules the operator could see in the Rules
    tab. So this says how many rules of the subtype exist, and names the ones of other
    subtypes that are left out on purpose (a rule of another subtype cannot fire here).
    """
    rules = list(rules or ())
    if str(query or '').strip() and addable_rules(rules, subtype, already):
        return 'No rule matches that search.'
    label = subtype_label(subtype)
    same = sum(1 for rule in rules if matches_subtype(rule, subtype))
    if same == 0:
        text = f'There is no {label} rule yet.'
    elif same == 1:
        text = f'The only {label} rule is already in this ruleset.'
    else:
        text = f'All {same} {label} rules are already in this ruleset.'
    others: dict = {}
    for rule in rules:
        if not matches_subtype(rule, subtype):
            other = subtype_label(subtype_of(rule)) or 'no-subtype'
            others[other] = others.get(other, 0) + 1
    if others:
        listed = ', '.join(_count(n, f'{name} rule') for name, n in sorted(others.items()))
        text += (f' Not offered: {listed}, because this ruleset only evaluates {label} rules.'
                 f' To add another, create one with the {label} subtype in the Rules tab.')
    return text


def _haystack(rule: Any) -> str:
    """Everything about a rule an operator might type: its name and its sentences.

    The gates and actions come from ``build_rule_views`` -- the same formatter the cards
    and the transaction-details dialog print -- so a search matches what is ON SCREEN.
    Searching the raw ``triggers`` dict instead would match key spellings nobody is shown.
    """
    view, = build_rule_views([rule])
    return ' '.join((view.name, *view.when, *view.then)).lower()


def search_rules(rules: Iterable[Any], query: Optional[str]) -> List[Any]:
    """``rules`` narrowed to those matching ``query``. An empty query narrows nothing.

    Rules are NAMED by people and GATED in the engine's vocabulary, and an operator
    hunting "the one that moves the stop" has only the second to go on -- so both are
    searched. Words are matched independently so "stop loss" finds ``adjust_stop_loss``.
    """
    words = str(query or '').lower().split()
    if not words:
        return list(rules or ())
    # The haystack is built ONCE per rule. Inlining it into the ``all(...)`` would rebuild
    # every rule's views once per word, on every keystroke, over the whole candidate list.
    matches = []
    for rule in (rules or ()):
        text = _haystack(rule)
        if all(word in text for word in words):
            matches.append(rule)
    return matches


def moved(order: Sequence[int], index: int, delta: int) -> List[int]:
    """``order`` with the entry at ``index`` swapped one place by ``delta``. A new list.

    An arrow at the end of the list moves NOTHING: the first rule has nothing above it,
    and wrapping around would be a re-rank nobody asked for, on the one control whose
    entire job is to be deliberate.
    """
    target = index + delta
    if not (0 <= index < len(order)) or not (0 <= target < len(order)):
        return list(order)
    result = list(order)
    result[index], result[target] = result[target], result[index]
    return result


def partition_by_subtype(rules: Iterable[Any],
                         subtype: Optional[str]) -> Tuple[List[Any], List[Any]]:
    """``(kept, dropped)`` for a subtype change, both in the order given.

    The dropped rules are RETURNED rather than counted so the caller can name them. The
    dialog lets the subtype be changed after rules are listed, and a rule that no longer
    matches has two bad endings: kept, it looks live and can never fire; dropped in
    silence, the ruleset loses a rule between opening and saving and nothing says so.
    """
    kept, dropped = [], []
    for rule in (rules or ()):
        (kept if matches_subtype(rule, subtype) else dropped).append(rule)
    return kept, dropped
